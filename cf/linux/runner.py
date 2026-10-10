"""Experiment loop for the Ubuntu back-end: switch windows, throttle whatever is not in front.

For every request r_t of the trace:
  1. lift all limits from r_t (tier fg), bring its window to the foreground (start it when it is
     not running) and measure activate_ms / ping_ms (-> total_time_ms, the "resume latency")
  2. settle, sample every app (PSS, swap, cpu.stat, io, cgroup limits) and the system
  3. policy -> resident set S_t under B = eta * sum m_fg:
         r_t                      fg      no limits
         i in S_t, i != r_t       bg      CPU limit only (cpu_throttle_resident) or nothing
         i not in S_t             shrunk  CPU limit + memory.high = mem_frac * m_fg_i (+ swap cap)
     `static` = everything but r_t shrunk; `none` = nothing limited (baseline)
  4. dwell, sample again; per background app record what it still achieved while limited
     (cpu time, throttled time, bytes written, notifications, major faults) and whether any
     process / window disappeared or an OOM kill happened (state_lost - must stay empty)
  5. write one JSON line, checkpoint

The checkpoint / resume mechanics are inherited from cf.runner.Experiment (same JSONL layout,
same CLI behaviour: --resume continues after Ctrl+C or a crash with the same trace).
"""
from __future__ import annotations

import os
import time

from ..logging_util import fmt_dur, short_pkg
from ..policies import AppInfo, residency
from ..runner import Experiment, PreflightError, RESUMABLE_ERRORS, CKPT_VERSION
from ..scenarios import display_name
from . import apps as A
from .cgroup import ThrottleProfile
from .device import LinuxDesktop

DEFAULTS = {
    "policy": "landlord",
    "policy_kw": {},
    "eta": 0.3,
    "trace": {"kind": "scenario", "scenario": "office", "T": 60, "seed": 1},
    "dwell_s": 8.0,
    "settle_s": 2.0,
    "warmup_dwell_s": 10.0,
    "throttle": ThrottleProfile().to_dict(),
    "cpu_throttle_resident": True,     # residents (in S_t) also get the CPU limit (tier bg)
    "never_throttle": [],              # always tier fg (music player, screen recorder ...)
    "minimize_background": False,
    "default_rho": None,               # None: zram -> measured compression ratio, disk swap -> 0
    "out_dir": "results",
    "fixed_m_fg_kb": None,
    "fixed_a_fg_kb": None,
    "strict_preflight": True,
    "sudo_adopt": False,
    "adopt_strays": True,
    "app_overrides": {},
    "notifications": True,
    "launch_timeout_s": 90.0,
    "ping_timeout_s": 15.0,
    "min_ram_mb": 6144,
}

LINUX_CKPT_VERSION = CKPT_VERSION * 100 + 1    # distinct from the Android checkpoints


class ThrottleExperiment(Experiment):
    def __init__(self, cfg: dict, dev: LinuxDesktop, log=None, resume: bool = False):
        merged = {**DEFAULTS, **cfg}
        merged["trace"] = {**DEFAULTS["trace"], **cfg.get("trace", {})}
        merged["throttle"] = {**DEFAULTS["throttle"], **(cfg.get("throttle") or {})}
        super().__init__(merged, dev, log=log, resume=resume)
        self.cfg = merged                    # drop the Android defaults the base class merged in
        self.dev: LinuxDesktop = dev
        self.profile = ThrottleProfile.from_cfg(merged["throttle"])
        dev.profile = self.profile
        self.throttled: set[str] = set()     # tier bg or shrunk (CPU-limited)
        self.shrunk: set[str] = set()        # tier shrunk (memory.high)
        self.tiers: dict[str, str] = {}
        self.substitutions: list[tuple[str, str]] = []
        self.categories: dict[str, str] = {}
        self.prev_notif: dict = {}

    # ------------------------------------------------------------ checkpoint
    def save_checkpoint(self) -> None:
        import pickle
        state = {
            "version": LINUX_CKPT_VERSION, "name": self.cfg["name"], "cfg": self.cfg, "apps": self.apps,
            "m_fg": self.m_fg, "a_fg": self.a_fg, "l_cold": self.l_cold, "B_kb": self.B_kb,
            "trace": self.trace, "t_done": self.t_done, "policy": self.pol, "current": self.current,
            "throttled": self.throttled, "shrunk": self.shrunk, "tiers": self.tiers,
            "resume_hist": self.resume_hist, "prev_pids": self.prev_pids,
            "substitutions": self.substitutions, "categories": self.categories, "saved_at": time.time(),
        }
        tmp = self.ckpt_path + ".tmp"
        with open(tmp, "wb") as f:
            pickle.dump(state, f)
        os.replace(tmp, self.ckpt_path)

    def _restore(self, state: dict) -> None:
        from ..runner import ResumeError
        if state.get("version") != LINUX_CKPT_VERSION:
            raise ResumeError(f"checkpoint version {state.get('version')} != {LINUX_CKPT_VERSION} "
                              f"(an Android checkpoint? use run_experiment.py for those)")
        saved = state["cfg"]
        for k in ("policy", "eta", "trace", "throttle"):
            if saved.get(k) != self.cfg.get(k) and k in self.cfg and self.cfg.get(k) not in (None, "?"):
                self.log.warn(f"resume: config '{k}' differs from checkpoint; using checkpoint value")
        self.cfg = saved
        self.profile = ThrottleProfile.from_cfg(saved["throttle"])
        self.dev.profile = self.profile
        self.apps = state["apps"]
        self.m_fg, self.a_fg, self.l_cold = state["m_fg"], state["a_fg"], state["l_cold"]
        self.B_kb, self.trace, self.t_done = state["B_kb"], state["trace"], state["t_done"]
        self.pol, self.current = state["policy"], set(state["current"])
        self.throttled, self.shrunk = set(state["throttled"]), set(state["shrunk"])
        self.tiers = dict(state["tiers"])
        self.resume_hist, self.prev_pids = state["resume_hist"], state["prev_pids"]
        self.substitutions = state.get("substitutions", [])
        self.categories = state.get("categories", {})
        self.step_times = []

    def _reapply_device_state(self) -> None:
        dev = self.dev
        dev.release_all()
        s = dev.sample(self.apps, with_windows=True)
        alive = {p for p in self.apps if s["apps"][p].alive}
        gone = sorted((self.throttled | self.shrunk) - alive)
        if gone:
            self.log.warn(f"resume: apps no longer running since the checkpoint (will be COLD): {gone}")
        self.throttled &= alive
        self.shrunk &= alive
        for app in self.apps:
            tier = self.tiers.get(app, "fg") if app in alive else "fg"
            if tier != "fg":
                dev.set_tier(app, tier, self.m_fg.get(app, 0) * 1024)
            self.tiers[app] = tier
        self.log.info(f"resume: re-applied tiers - throttled {len(self.throttled)}, shrunk {len(self.shrunk)}, "
                      f"running {len(alive)}/{len(self.apps)}")

    # ------------------------------------------------------------- lifecycle
    def prepare(self) -> None:
        dev = self.dev
        dev.cat = A.with_overrides(self.cfg.get("app_overrides"))
        dev.sudo_adopt = bool(self.cfg["sudo_adopt"])
        dev.launch_timeout = float(self.cfg["launch_timeout_s"])
        dev.ping_timeout = float(self.cfg["ping_timeout_s"])
        if not self.cfg["notifications"]:
            dev.notify = None
        caps = dev.probe()
        self.log.head(f"host: {caps.get('distro')} {caps.get('kernel')} {caps.get('arch')}, "
                      f"RAM {caps.get('mem_total_kb', 0) // 1024} MB, swap {caps.get('swap_total_kb', 0) // 1024} MB, "
                      f"{caps.get('cpu_count')} cpu, vm: {caps.get('hypervisor') or '?'}")
        self.log.info(f"cgroup {caps.get('cgroup_base')}: available={caps.get('cgroup_available')} "
                      f"writable={caps.get('cgroup_writable')} runner_inside={caps.get('runner_inside_cf')} "
                      f"controllers={caps.get('subtree_control')} memory.reclaim={caps.get('memory_reclaim')}")
        self.log.info(f"display {caps.get('display') or '-'} ({caps.get('session_type') or '?'}), wm {caps.get('wm') or '?'}, "
                      f"ping={caps.get('ping_available')} notifications={caps.get('notify_monitor', False)}")
        self.log.info("throttle profile: " + self.profile.describe()
                      + (" | residents CPU-limited too" if self.cfg["cpu_throttle_resident"] else " | residents untouched"))
        self._preflight(caps)
        if self.resume:
            return
        used, subs, missing = A.resolve(self.cfg["apps"], dev.cat)
        for want, got in subs:
            self.log.warn(f"{display_name(want)} ({want}) is not installed - using {display_name(got)} ({got}) instead")
        for m in missing:
            self.log.warn(f"{display_name(m)} ({m}) not installed and no substitute available - dropped")
        if len(used) < 2:
            raise RuntimeError("need at least two runnable apps (see --probe / --catalog)")
        self.apps = used
        self.substitutions = subs
        self.categories = {a: A.category_of(a, dev.cat) for a in used}
        # scenario traces are defined over catalogue ids; apply the substitutions inside them
        tr = A.resolve_trace_cfg(self.cfg["trace"])
        mapping = {w: g for w, g in subs}
        for m in missing:
            mapping[m] = None
        if tr.get("kind") == "scenario" and isinstance(tr.get("scenario"), dict):
            tr["scenario"] = A.remap_scenario_apps(tr["scenario"], mapping)
        elif tr.get("kind") == "day":
            tr["schedule"] = [[A.remap_scenario_apps(sc, mapping), share] for sc, share in tr["schedule"]]
        self.cfg["trace"] = tr
        self.log.info(f"apps ({len(used)}): " + ", ".join(f"{display_name(a)}[{self.categories[a][0]}]" for a in used))
        for app in self.apps:
            dev.tree.ensure(app)
            if self.cfg["adopt_strays"]:
                r = dev.adopt(app)
                if r["moved"]:
                    self.log.info(f"{app}: adopted running processes {r['moved']} into {dev.tree.path(app)}")
                if r["failed"]:
                    self.log.warn(f"{app}: processes outside its cgroup could not be moved {r['failed']} - "
                                  f"limits will not apply to them (snap? started before setup? see manual §6)")
        self.emit({"type": "meta", "config": self.cfg, "apps": self.apps, "caps": dev.caps_dict(),
                   "backend": "linux", "categories": self.categories, "substitutions": self.substitutions,
                   "profile": self.profile.to_dict(),
                   "device": {"model": caps.get("hypervisor"), "build": f"{caps.get('distro')} {caps.get('kernel')}"}})

    def _preflight(self, caps: dict) -> None:
        blocking: list[str] = []
        if not caps.get("cgroup_available"):
            blocking.append(f"{caps.get('cgroup_base')} does not exist or has no controllers. Run once per boot: "
                            f"sudo scripts/ubuntu/setup_cgroups.sh  (then start the runner from THAT shell)")
        else:
            if not caps.get("cgroup_writable"):
                blocking.append(f"{caps.get('cgroup_base')} is not writable by uid {os.getuid()}: re-run "
                                f"sudo scripts/ubuntu/setup_cgroups.sh (it chowns the subtree to $SUDO_USER)")
            if not caps.get("runner_inside_cf"):
                blocking.append(f"this process is in cgroup {caps.get('runner_cgroup')!r}, not under /cf/. Apps it "
                                f"starts could not be moved into /cf/<app> (common-ancestor rule). Start the runner "
                                f"from the shell that ran `sudo scripts/ubuntu/setup_cgroups.sh`, or `source "
                                f"scripts/ubuntu/cf_enter.sh`")
            missing = [c for c in ("cpu", "memory") if c not in caps.get("subtree_control", [])]
            if self.profile.cpu_mode in ("cpuset", "both") and "cpuset" not in caps.get("subtree_control", []):
                missing.append("cpuset")
            if missing:
                blocking.append(f"controllers {missing} not enabled in {caps.get('cgroup_base')}/cgroup.subtree_control "
                                f"(setup_cgroups.sh enables cpu cpuset memory io)")
        if not caps.get("display"):
            blocking.append("no DISPLAY: run inside the graphical session (a terminal on the Ubuntu desktop)")
        elif caps.get("session_type") == "wayland":
            blocking.append("Wayland session: xdotool cannot switch windows of native Wayland clients. Log out and pick "
                            "'Ubuntu on Xorg' at the login screen (gear icon)")
        if caps.get("x11_tools_missing"):
            blocking.append(f"missing tools {caps['x11_tools_missing']}: sudo apt install xdotool x11-utils")
        if not caps.get("ping_available"):
            self.log.warn(f"_NET_WM_PING unavailable ({caps.get('ping_error')}): sudo apt install python3-xlib; "
                          f"total_time_ms will only contain the WM switch time")
        if self.cfg["notifications"] and not caps.get("notify_monitor"):
            self.log.warn(f"notification monitor not running ({caps.get('notify_error')}); push metrics will be 0")
        if not caps.get("swaps") and self.profile.mem_frac < 1.0 and self.cfg["policy"] != "none":
            self.log.warn("no swap device: memory.high can only drop file pages, anonymous memory stays - shrunk apps "
                          "will be allocation-throttled instead of shrunk. Run scripts/ubuntu/setup_zram.sh")
        if caps.get("cgroup_available") and not caps.get("memory_reclaim"):
            self.log.warn("kernel without memory.reclaim (< 5.19): shrinking happens lazily when the app allocates; "
                          "install the HWE kernel (linux-generic-hwe-22.04) for proactive reclaim")
        if caps.get("mem_total_kb", 0) // 1024 < self.cfg["min_ram_mb"]:
            self.log.warn(f"VM RAM is {caps.get('mem_total_kb', 0) // 1024} MB (< {self.cfg['min_ram_mb']}); with a dozen "
                          f"desktop apps the kernel's own reclaim will dominate the measurements")
        for msg in blocking:
            (self.log.err if self.cfg["strict_preflight"] else self.log.warn)(msg)
        if blocking and self.cfg["strict_preflight"]:
            raise PreflightError(f"{len(blocking)} blocking preflight problem(s); fix them or use --no-strict")

    def warmup(self) -> float:
        dev = self.dev
        self.log.head(f"warmup: bring up {len(self.apps)} apps (no restarts - logins are kept), dwell "
                      f"{self.cfg['warmup_dwell_s']}s each")
        t0 = time.time()
        for i, app in enumerate(self.apps):
            res = dev.launch(app)
            self.tiers[app] = "fg"
            time.sleep(self.cfg["warmup_dwell_s"])
            s = dev.sample(self.apps)
            smp = s["apps"][app]
            self.m_fg[app] = smp.total("pss_kb") + smp.total("swap_pss_kb")
            self.a_fg[app] = self.m_fg[app]
            fixed = self.cfg.get("fixed_m_fg_kb") or {}
            if app in fixed:
                self.m_fg[app] = int(fixed[app])
                self.a_fg[app] = int((self.cfg.get("fixed_a_fg_kb") or {}).get(app, self.m_fg[app]))
            self.l_cold[app] = res.get("total_time_ms") if res.get("launch_state") == "COLD" else None
            self.emit({"type": "warmup", "pkg": app, "launch": res, "app": smp.to_dict(), "system": s["system"]})
            eta = (time.time() - t0) / (i + 1) * (len(self.apps) - i - 1)
            self.log.info(f"[warmup {i + 1:2d}/{len(self.apps)} ETA {fmt_dur(eta):>6}] {display_name(app):<14} "
                          f"PSS {self.m_fg[app] // 1024:5d} MB  {str(res.get('launch_state')):>6} "
                          f"{str(res.get('total_time_ms')):>6} ms (activate {res.get('activate_ms')}, ping {res.get('ping_ms')})"
                          + (f"  pids {len(smp.procs)}" if smp.alive else "  NO PROCESS"))
            if not smp.alive or self.m_fg[app] == 0:
                self.log.warn(f"{app}: no process in its cgroup after launch - check cmd / match with --discover")
            elif res.get("status") not in ("ok",):
                self.log.warn(f"{app}: launch status {res.get('status')} - the window may be behind a dialog")
            if smp.stray_pids:
                self.log.warn(f"{app}: {len(smp.stray_pids)} matching process(es) outside its cgroup {smp.stray_pids[:5]}")
        self.B_kb = self.cfg["eta"] * sum(self.m_fg.values())
        fixed = self.cfg.get("fixed_m_fg_kb") or {}
        source = "fixed" if fixed and all(p in fixed for p in self.apps) else ("mixed" if fixed else "measured")
        self.emit({"type": "budget", "eta": self.cfg["eta"], "budget_kb": self.B_kb, "m_fg_source": source,
                   "sum_m_fg_kb": sum(self.m_fg.values()), "m_fg_kb": self.m_fg, "a_fg_kb": self.a_fg})
        self.log.ok(f"budget B = {self.cfg['eta']} x {sum(self.m_fg.values()) // 1024} MB = {int(self.B_kb) // 1024} MB "
                    f"(m_fg {source}); shrunk apps get memory.high = "
                    + ", ".join(f"{display_name(a)} {self.profile.mem_high_bytes(self.m_fg[a] * 1024) >> 20}"
                                for a in self.apps) + " MB")
        return self.B_kb

    # ------------------------------------------------------------------ step
    def _rho(self, sample: dict) -> float:
        if self.cfg.get("default_rho") is not None:
            return float(self.cfg["default_rho"])
        z = sample["system"].get("zram") or {}
        if z.get("orig_data_size", 0) > 64 * 2 ** 20:
            return max(0.05, min(1.0, z["mem_used_total"] / z["orig_data_size"]))
        swaps = sample["system"].get("swaps") or []
        if any("zram" in s["filename"] for s in swaps):
            return 0.35
        return 0.0 if swaps else 1.0

    def _infos(self, sample: dict, rho: float) -> dict[str, AppInfo]:
        infos = {}
        for app in self.apps:
            smp = sample["apps"][app]
            m = (smp.total("pss_kb") + smp.total("swap_pss_kb")) * 1024 if smp.alive else self.m_fg[app] * 1024
            m = max(m, 1.0)
            high = self.profile.mem_high_bytes(self.m_fg[app] * 1024)
            rho_i = min(1.0, high / m)
            hist = self.resume_hist.get(app, [])
            comp = [ms for c, ms in hist if c and ms]
            hot = [ms for c, ms in hist if not c and ms]
            if comp and hot:
                c_resume = max(1.0, sum(comp) / len(comp) - sum(hot) / len(hot))
            else:
                c_resume = 60.0 + max(0.0, m - high) / (1.2 * 2 ** 30) * 1000   # ms; ~1.2 GB/s page-in
            infos[app] = AppInfo(app, m, m, rho_i, c_resume, 0.0)
        return infos

    def _tier_for(self, app: str, resident: bool) -> str:
        if app in self.cfg["never_throttle"]:
            return "fg"
        if resident:
            return "bg" if self.cfg["cpu_throttle_resident"] else "fg"
        return "shrunk"

    def _apply(self, req: str, S: set[str], sample: dict, infos: dict[str, AppInfo]) -> dict:
        dev = self.dev
        actions = {"freeze": [], "unfreeze": [], "reclaim": {}, "tiers": {}, "limits": {}}
        for app in self.apps:
            if app == req or not sample["apps"][app].alive:
                continue
            tier = self._tier_for(app, app in S)
            cur = self.tiers.get(app, "fg")
            if tier != cur or (tier == "shrunk" and app not in self.shrunk):
                r = dev.set_tier(app, tier, self.m_fg[app] * 1024)
                actions["tiers"][app] = tier
                actions["limits"][app] = r.get("limits")
                if tier == "shrunk":
                    actions["reclaim"][app] = r.get("reclaim") or "memory.high"
                if tier != "fg" and cur == "fg":
                    actions["freeze"].append((app, tier))
                elif tier == "fg" and cur != "fg":
                    actions["unfreeze"].append((app, "fg"))
                if self.cfg["minimize_background"] and tier != "fg":
                    dev.minimize(app)
            self.tiers[app] = tier
            if tier == "fg":
                self.throttled.discard(app); self.shrunk.discard(app)
            elif tier == "bg":
                self.throttled.add(app); self.shrunk.discard(app)
            else:
                self.throttled.add(app); self.shrunk.add(app)
        return actions

    @staticmethod
    def _bg_delta(a: dict, b: dict, wall_s: float) -> dict:
        d = {"cpu_ms": max(0, b["cpu_ms"] - a["cpu_ms"]),
             "cg_cpu_ms": max(0, b["cg_usage_ms"] - a["cg_usage_ms"]),
             "throttled_ms": max(0, b["cg_throttled_ms"] - a["cg_throttled_ms"]),
             "nr_throttled": max(0, b["cg_nr_throttled"] - a["cg_nr_throttled"]),
             "write_mb": round(max(0, b["write_bytes"] - a["write_bytes"]) / 2 ** 20, 2),
             "read_mb": round(max(0, b["read_bytes"] - a["read_bytes"]) / 2 ** 20, 2),
             "majflt": max(0, b["majflt"] - a["majflt"]),
             "high_events": max(0, (b["cg_events"].get("high", 0) - a["cg_events"].get("high", 0))),
             "mem_mb": b["cg_mem_mb"], "swap_mb": b["cg_swap_mb"], "wall_s": round(wall_s, 2)}
        d["cores"] = round(d["cg_cpu_ms"] / 1000 / wall_s, 3) if wall_s > 0 else None
        return d

    def step(self, t: int, req: str) -> dict:
        B = self.B_kb * 1024
        pol = self.pol
        t_step = time.time()
        pol.on_request(t, req)
        was_shrunk = req in self.shrunk
        was_throttled = req in self.throttled
        if self.tiers.get(req, "fg") != "fg":
            self.dev.set_tier(req, "fg")
        self.tiers[req] = "fg"
        self.throttled.discard(req)
        self.shrunk.discard(req)
        launch = self.dev.launch(req)
        if was_shrunk and launch.get("launch_state") == "HOT":
            # process never left memory, but came back from a memory.high-shrunk tier (page-ins expected)
            launch["launch_state"] = "WARM"
        time.sleep(self.cfg["settle_s"])
        s1 = self.dev.sample(self.apps)
        rho = self._rho(s1)
        infos = self._infos(s1, rho)
        if launch.get("total_time_ms"):
            self.resume_hist.setdefault(req, []).append((was_shrunk, launch["total_time_ms"]))
        t_dec = time.time()
        S = set(pol.choose_resident(t, req, infos, B, self.current)) | {req} | set(self.cfg["never_throttle"])
        decision_ms = (time.time() - t_dec) * 1000
        t_act = time.time()
        actions = self._apply(req, S, s1, infos)
        action_ms = (time.time() - t_act) * 1000
        self.current = S
        t_dwell = time.time()
        time.sleep(self.cfg["dwell_s"])
        s2 = self.dev.sample(self.apps)
        wall = time.time() - t_dwell
        d1 = {p: s1["apps"][p].to_dict() for p in self.apps}
        d2 = {p: s2["apps"][p].to_dict() for p in self.apps}
        bg, state_lost, notif = {}, {}, {}
        for p in self.apps:
            if d1[p]["alive"]:
                bg[p] = self._bg_delta(d1[p], d2[p], wall)
                bg[p]["tier"] = self.tiers.get(p, "fg")
                n = self.dev.notifications_for(p, s1.get("notifications", {}), s2.get("notifications", {}))
                bg[p]["notifications"] = n
                if n:
                    notif[p] = n
            lost = LinuxDesktop.state_check(d1[p], d2[p])
            if lost:
                state_lost[p] = lost
        pids_now = {p: set(d2[p]["pids"]) for p in self.apps}
        killed = [p for p in self.apps if self.prev_pids.get(p) and not pids_now[p]]
        self.prev_pids = pids_now
        M_bg = sum((d2[p]["pss_kb"] + rho * d2[p]["swap_pss_kb"]) * 1024 for p in self.apps if p != req)
        M_model = residency(S, self._infos(s2, rho), req)
        rec = {"type": "step", "t": t, "req": req, "launch": launch,
               "was_compressed": was_shrunk, "was_frozen": was_throttled,
               "was_shrunk": was_shrunk, "was_throttled": was_throttled,
               "resident": sorted(S), "frozen": sorted(self.throttled), "compressed": sorted(self.shrunk),
               "throttled": sorted(self.throttled), "shrunk": sorted(self.shrunk), "tiers": dict(self.tiers),
               "actions": actions, "rho_est": rho, "budget_kb": self.B_kb, "M_bg_kb": M_bg / 1024,
               "M_model_kb": M_model / 1024, "budget_violation": M_bg > B,
               "decision_ms": round(decision_ms, 3), "action_ms": round(action_ms, 1),
               "step_s": round(time.time() - t_step, 2), "killed_since_prev": killed, "kill_info": {},
               "state_lost": state_lost, "bg": bg, "notifications": notif,
               "after_launch": {"system": s1["system"], "apps": d1},
               "after_dwell": {"system": s2["system"], "apps": d2},
               "tau_hat": pol.predicted_next_use(req)}
        self._log_step(rec, t_step)
        return rec

    def _log_step(self, rec: dict, t_step: float) -> None:
        T = len(self.trace)
        self.step_times.append(time.time() - t_step)
        recent = self.step_times[-10:]
        eta_s = sum(recent) / len(recent) * (T - rec["t"] - 1)
        la = rec["launch"]
        src = "shr" if rec["was_shrunk"] else ("bg" if rec["was_throttled"] else "fg")
        bg = rec["bg"]
        others = [b for p, b in bg.items() if p != rec["req"]]
        cores = sum(b["cores"] or 0 for b in others)
        thr = sum(b["throttled_ms"] for b in others) / 1000
        dl = sum(b["write_mb"] for p, b in bg.items() if self.categories.get(p) == "download" and p != rec["req"])
        ntf = sum(rec["notifications"].values())
        mbg, b = int(rec["M_bg_kb"] / 1024), int(rec["budget_kb"] / 1024)
        viol = " VIOL" if rec["budget_violation"] else ""
        lost = ""
        if rec["state_lost"]:
            lost = " | STATE LOST: " + ",".join(f"{short_pkg(k)}{sorted(v)}" for k, v in rec["state_lost"].items())
        line = (f"[{rec['t'] + 1:3d}/{T} {fmt_dur(time.time() - self.t_start):>6} ETA {fmt_dur(eta_s):>6}] "
                f"{display_name(rec['req']):<10} {str(la.get('launch_state') or '?'):>6} {str(la.get('total_time_ms')):>5} ms "
                f"(act {la.get('activate_ms')} ping {la.get('ping_ms')}) <-{src:<3} "
                f"| S={len(rec['resident']):2d} bg={len(rec['throttled']):2d} shr={len(rec['shrunk']):2d} "
                f"| M_bg {mbg:5d}/{b} MB{viol} | bg {cores:.2f} cores thr {thr:.1f}s dl {dl:.1f}MB ntf {ntf} "
                f"| {len(rec['actions']['tiers'])} tier changes {rec['action_ms'] / 1000:.1f}s{lost}")
        (self.log.warn if rec["state_lost"] or rec["killed_since_prev"] else self.log.step)(line)

    # ------------------------------------------------------------------- run
    def cleanup(self) -> None:
        try:
            self.dev.release_all()
        except RESUMABLE_ERRORS:
            pass
        self.throttled.clear()
        self.shrunk.clear()
        self.tiers = {a: "fg" for a in self.apps}

    def close(self) -> None:
        super().close()
        try:
            self.dev.close()
        except Exception:    # noqa: BLE001
            pass

    def _final_summary(self) -> None:
        try:
            from .analyze import summarize
            from ..analyze import load
            r = summarize(load(self.path))
        except Exception as e:    # noqa: BLE001
            self.log.warn(f"summary failed: {e}")
            return
        self.log.head(f"done {self.cfg['name']}: {r['T']} steps in {fmt_dur(time.time() - self.t_start)} -> {self.path}")
        self.log.ok(f"switches: HOT {r['n_hot']} WARM(from shrunk) {r['n_warm']} COLD(started) {r['n_cold']} "
                    f"TIMEOUT {r['n_timeout']} FRONT {r['n_front']} | processes gone {r['n_killed']} | "
                    f"state lost events {r['state_lost_events']} oom kills {r['oom_kills']}")
        self.log.ok(f"switch latency P50/P95/P99 {r['lat_p50_ms']}/{r['lat_p95_ms']}/{r['lat_p99_ms']} ms "
                    f"(ping P50 resident {r['ping_resident_p50_ms']} / shrunk {r['ping_shrunk_p50_ms']}, "
                    f"activate P50 {r['activate_p50_ms']})")
        self.log.ok(f"background PSS avg {r['bg_pss_avg_mb']} MB (peak {r['bg_pss_peak_mb']}) vs budget {r['budget_mb']} MB, "
                    f"violations {r['budget_violations']}/{r['T']}, achieved eta {r.get('bg_pss_avg_over_sum_fg')}")
        self.log.ok(f"background got {r['bg_cores_avg']} cores on average, throttled {r['bg_throttled_pct']}% of the time; "
                    f"downloads {r['download_mb']} MB ({r['download_mbps']} MB/s), notifications {r['notif_total']} "
                    f"({r['notif_per_h']}/h), compute cpu {r['compute_cpu_s']} s; swap write/read {r['swap_write_mb']}/{r['swap_read_mb']} MB")
        if r["state_lost_events"]:
            self.log.warn("state_lost > 0: a process / window disappeared or the kernel OOM-killed something while "
                          "throttled - inspect the step records (mem_hard? swap exhausted? app crashed?)")
        if self.log.warnings:
            self.log.warn(f"{len(self.log.warnings)} warning(s) during this run - see above / {self.path[:-6]}.log")

    def run(self) -> str:
        # the base run() generates the trace with cf.trace over self.cfg["trace"], which prepare()
        # has already rewritten with inline Linux scenarios
        return super().run()


def run_from_config(cfg: dict, verbose: bool = False, resume: bool = False, log=None) -> str:
    dev = LinuxDesktop(log=log, notifications=cfg.get("notifications", True))
    exp = ThrottleExperiment(cfg, dev, log=log, resume=resume)
    return exp.run()
