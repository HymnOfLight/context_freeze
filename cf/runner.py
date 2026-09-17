"""On-device experiment loop (Android emulator or rooted phone).

For every request r_t in the trace:
  1. unfreeze r_t, `am start -W` it (records resume latency + HOT/WARM/COLD)
  2. sample per-app memory (PSS anon/file, SwapPss) and system counters
  3. ask the policy for the resident set S_t under the budget B = eta * sum_i m_fg_i
  4. freeze every background app outside S_t and reclaim its anonymous pages
     (memcg memory.reclaim -> memcg v1 force_empty -> /proc/<pid>/reclaim -> tmpfs balloon)
  5. dwell, sample again, append one JSON line to the result file

Checkpoint / resume
  After every step the complete controller state (policy object, resident / frozen /
  compressed sets, resume-latency history, previous pids) is pickled to <result>.ckpt.
  `Experiment(cfg, dev, resume=True).run()` (CLI: `--resume`) reopens the JSONL in append
  mode, replays nothing, re-applies the frozen / compressed state to the device and
  continues with step t+1.  Emulator crashes, adb timeouts and Ctrl+C all leave a
  consistent checkpoint behind.
"""
from __future__ import annotations

import json
import os
import pickle
import subprocess
import time
from typing import Optional

from .adb import Adb, AdbError
from .device import Device
from .logging_util import Logger, as_logger, fmt_dur, short_pkg
from .policies import AppInfo, make_policy, residency
from .trace import make_trace

DEFAULTS = {
    "policy": "landlord",
    "policy_kw": {},
    "eta": 0.3,
    "trace": {"kind": "zipf", "T": 40, "seed": 1, "s": 1.0, "stickiness": 0.3},
    "dwell_s": 4.0,
    "settle_s": 1.5,
    "warmup_dwell_s": 6.0,
    "freeze_resident": True,
    "reclaim_mode": "anon",
    "zram_mb": 1024,
    "zram_algo": None,
    "swapfile_mb": 0,
    "balloon_reserve_mb": 350,
    "balloon_max_mb": 2048,
    "stop_lmkd": False,
    "never_freeze": [],
    "default_rho": 0.35,
    "out_dir": "results",
    "min_guest_ram_mb": 4096,      # below this lmkd kills dominate the COLD count (preflight warning)
    "fixed_m_fg_kb": None,         # {pkg: kB}: reuse one warmup's m_fg so every run of a matrix gets the same budget
    "fixed_a_fg_kb": None,
    "strict_preflight": True,      # abort instead of warn when a known confounder is present (see _preflight)
}

CKPT_VERSION = 1

# errors after which a re-run with --resume makes sense (device / adb went away)
RESUMABLE_ERRORS = (AdbError, subprocess.TimeoutExpired, subprocess.CalledProcessError,
                    ConnectionError, OSError)


class ResumeError(RuntimeError):
    pass


class PreflightError(RuntimeError):
    """The device is in a state that makes the run's numbers meaningless (system freezer active,
    software GPU ...). Fix the device or pass --no-strict to run anyway."""


class Experiment:
    def __init__(self, cfg: dict, dev: Device, log=None, resume: bool = False):
        self.cfg = {**DEFAULTS, **cfg}
        self.cfg["trace"] = {**DEFAULTS["trace"], **cfg.get("trace", {})}
        self.dev = dev
        self.log: Logger = as_logger(log)
        self.resume = resume
        self.apps: list[str] = []
        self.m_fg: dict[str, int] = {}      # kB, foreground reference working set
        self.a_fg: dict[str, int] = {}      # kB, anon part
        self.l_cold: dict[str, Optional[int]] = {}
        self.frozen: set[str] = set()
        self.compressed: set[str] = set()
        self.resume_hist: dict[str, list[tuple[bool, int]]] = {}   # (was_compressed, ms)
        self.prev_pids: dict[str, set[int]] = {}
        self.trace: list[str] = []
        self.pol = None
        self.current: set[str] = set()
        self.B_kb: float = 0.0
        self.t_done: int = -1               # last step written to the JSONL
        self.step_times: list[float] = []
        self.fh = None
        self.path = ""
        self.t_start = time.time()

    # ------------------------------------------------------------- paths / io
    def result_path(self) -> str:
        name = self.cfg.get("name") or f"{self.cfg['policy']}_eta{self.cfg['eta']}_{int(time.time())}"
        self.cfg["name"] = name
        return os.path.join(self.cfg["out_dir"], name + ".jsonl")

    @property
    def ckpt_path(self) -> str:
        return self.path[:-len(".jsonl")] + ".ckpt" if self.path.endswith(".jsonl") else self.path + ".ckpt"

    def open_log(self) -> str:
        os.makedirs(self.cfg["out_dir"], exist_ok=True)
        self.path = self.result_path()
        self.fh = open(self.path, "a" if self.resume else "w", encoding="utf-8")
        if self.log.sink is None:
            self.log.attach(self.path[:-len(".jsonl")] + ".log", append=self.resume)
        return self.path

    def emit(self, rec: dict) -> None:
        rec.setdefault("ts", time.time())
        self.fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.fh.flush()

    # ------------------------------------------------------------- checkpoint
    def save_checkpoint(self) -> None:
        state = {
            "version": CKPT_VERSION, "name": self.cfg["name"], "cfg": self.cfg, "apps": self.apps,
            "m_fg": self.m_fg, "a_fg": self.a_fg, "l_cold": self.l_cold, "B_kb": self.B_kb,
            "trace": self.trace, "t_done": self.t_done, "policy": self.pol, "current": self.current,
            "frozen": self.frozen, "compressed": self.compressed, "resume_hist": self.resume_hist,
            "prev_pids": self.prev_pids, "saved_at": time.time(),
        }
        tmp = self.ckpt_path + ".tmp"
        with open(tmp, "wb") as f:
            pickle.dump(state, f)
        os.replace(tmp, self.ckpt_path)      # atomic: a crash mid-write never corrupts the old one

    @staticmethod
    def load_checkpoint(jsonl_path: str) -> Optional[dict]:
        p = jsonl_path[:-len(".jsonl")] + ".ckpt" if jsonl_path.endswith(".jsonl") else jsonl_path + ".ckpt"
        if not os.path.exists(p):
            return None
        with open(p, "rb") as f:
            return pickle.load(f)

    @staticmethod
    def jsonl_status(jsonl_path: str) -> dict:
        """{'exists', 'finished', 'steps', 'T'} of a result file - used by the CLI to decide resume/skip."""
        st = {"exists": os.path.exists(jsonl_path), "finished": False, "steps": 0, "T": None}
        if not st["exists"]:
            return st
        with open(jsonl_path, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue      # half-written last line after a crash
                if r.get("type") == "step":
                    st["steps"] = max(st["steps"], r["t"] + 1)
                elif r.get("type") == "trace":
                    st["T"] = len(r["trace"])
                elif r.get("type") == "end":
                    st["finished"] = True
        return st

    def _restore(self, state: dict) -> None:
        if state.get("version") != CKPT_VERSION:
            raise ResumeError(f"checkpoint version {state.get('version')} != {CKPT_VERSION}")
        saved_cfg = state["cfg"]
        for k in ("policy", "eta", "trace"):
            if saved_cfg.get(k) != self.cfg.get(k):
                self.log.warn(f"resume: config '{k}' differs from checkpoint "
                              f"({self.cfg.get(k)!r} vs {saved_cfg.get(k)!r}); using checkpoint value")
        self.cfg = saved_cfg
        self.apps = state["apps"]
        self.m_fg, self.a_fg, self.l_cold = state["m_fg"], state["a_fg"], state["l_cold"]
        self.B_kb, self.trace, self.t_done = state["B_kb"], state["trace"], state["t_done"]
        self.pol, self.current = state["policy"], set(state["current"])
        self.frozen, self.compressed = set(state["frozen"]), set(state["compressed"])
        self.resume_hist, self.prev_pids = state["resume_hist"], state["prev_pids"]
        self.step_times = []

    def _reapply_device_state(self) -> None:
        """After an interruption the device may be in any state (cleanup unfroze everything, or the
        emulator rebooted). Bring it back to what step t_done recorded."""
        dev = self.dev
        s = dev.sample(self.apps)
        alive = {p for p in self.apps if s["apps"][p].alive}
        dead_frozen = sorted(self.frozen - alive)
        dead_comp = sorted(self.compressed - alive)
        if dead_frozen or dead_comp:
            self.log.warn(f"resume: processes gone since checkpoint (will be COLD): "
                          f"{sorted(set(dead_frozen) | set(dead_comp))}")
        self.frozen &= alive
        self.compressed &= alive
        for pkg in self.apps:            # start from a clean, fully thawed state
            if pkg in alive:
                dev.set_frozen(pkg, False)
        n_rec = 0
        for pkg in sorted(self.compressed):
            if pkg in self.cfg["never_freeze"]:
                continue
            how = dev.reclaim(pkg, self.cfg["reclaim_mode"])
            n_rec += how not in ("no-process", "none", "balloon-needed")
        for pkg in sorted(self.frozen):
            if pkg not in self.cfg["never_freeze"]:
                dev.set_frozen(pkg, True)
        dev.home()
        time.sleep(1)
        self.log.info(f"resume: re-applied device state - frozen {len(self.frozen)}, "
                      f"re-compressed {n_rec}/{len(self.compressed)}, alive {len(alive)}/{len(self.apps)}")

    # ------------------------------------------------------------- lifecycle
    def prepare(self) -> None:
        dev, a = self.dev, self.dev.adb
        root = a.ensure_root()
        caps = dev.probe()
        self.log.head(f"device: kernel {caps.kernel}, sdk {caps.android_sdk}, RAM {caps.mem_total_kb // 1024} MB, "
                      f"{caps.cpu_count} cpu, root via {root}")
        self.log.info(f"freezer={caps.freezer} reclaim={caps.reclaim_methods} zram={caps.zram} "
                      f"swaps={[s['filename'] for s in caps.swaps]} cached_apps_freezer={caps.cached_apps_freezer!r}")
        if self.cfg["zram_mb"]:
            self.log.info("zram: " + dev.setup_zram(self.cfg["zram_mb"], self.cfg.get("zram_algo")))
        if self.cfg["swapfile_mb"]:
            self.log.info("swapfile: " + dev.setup_swapfile(self.cfg["swapfile_mb"]))
        dev.probe()
        if self.cfg["stop_lmkd"]:
            a.shell("stop lmkd")
            self.log.warn("lmkd stopped (kernel OOM killer is the only safety net now)")
        a.shell("svc power stayon true; input keyevent KEYCODE_WAKEUP; wm dismiss-keyguard")
        for k in ("window_animation_scale", "transition_animation_scale", "animator_duration_scale"):
            a.shell(f"settings put global {k} 0")
        self._preflight(caps)
        if self.resume:
            return
        # validate app list
        self.apps = []
        for pkg in self.cfg["apps"]:
            uid = dev.uid_of(pkg)
            if dev.launcher_activity(pkg) and uid is not None:
                self.apps.append(pkg)
                if not Device.is_isolated_uid(uid) and pkg not in self.cfg["never_freeze"]:
                    # e.g. Settings shares uid 1000 with system_server: observe only
                    self.log.warn(f"{pkg} runs under shared system uid {uid}; added to never_freeze")
                    self.cfg["never_freeze"] = list(self.cfg["never_freeze"]) + [pkg]
            else:
                self.log.warn(f"{pkg} not installed / no launcher activity - dropped")
        if len(self.apps) < 2:
            raise RuntimeError("need at least two launchable apps")
        self.log.info(f"apps ({len(self.apps)}): " + ", ".join(short_pkg(p) for p in self.apps))
        if "balloon" in caps.reclaim_methods and len(caps.reclaim_methods) == 1:
            dev.balloon_setup(self.cfg["balloon_max_mb"])
        self.emit({"type": "meta", "config": self.cfg, "apps": self.apps, "caps": dev.caps_dict(),
                   "device": {"model": a.shell("getprop ro.product.model").strip(),
                              "build": a.shell("getprop ro.build.fingerprint").strip()}})

    def _preflight(self, caps) -> None:
        """Check for the confounders that spoiled earlier runs (docs/02 §6). Blocking ones raise
        PreflightError unless strict_preflight is off; the rest are warnings."""
        pol = self.cfg["policy"]
        blocking: list[str] = []
        if caps.cached_apps_freezer not in ("disabled", "false", "0"):
            blocking.append(f"Android's own cached-apps freezer is '{caps.cached_apps_freezer}' (null = device default = "
                            f"enabled on Android 15). It freezes/compacts/kills cached apps on its own and fights this "
                            f"controller for cgroup.freeze; 'none' then measures Android's freezer, not 'no freezing'. "
                            f"Fix: scripts/prepare_device.sh --system-freezer disabled (reboots), or --no-strict for "
                            f"an explicit 'Android default' baseline")
        gles = self.dev.renderer()
        if gles and Device.is_software_renderer(gles):
            blocking.append(f"emulator renders with a software GPU ({gles[:70]}...): every launch pays ~1 s of CPU "
                            f"rasterisation, so TotalTime measures rendering, not memory. Fix: start with "
                            f"scripts/start_emulator.sh (forces -gpu host) after freeing host RAM, or --no-strict")
        elif gles:
            self.log.info("renderer: " + gles[:110])
        if caps.mem_total_kb and caps.mem_total_kb // 1024 < self.cfg["min_guest_ram_mb"]:
            self.log.warn(f"guest RAM is only {caps.mem_total_kb // 1024} MB (< {self.cfg['min_guest_ram_mb']}); "
                          f"lmkd may kill background apps and inflate COLD starts")
        if not caps.swaps and pol != "none":
            self.log.warn("no swap device: compressed pages have nowhere to go (zram_mb in the config or "
                          "scripts/prepare_device.sh --zram-mb 1024)")
        if caps.reclaim_methods == ["balloon"] and pol != "none":
            self.log.warn("no per-app reclaim mechanism (memcg / proc reclaim); falling back to the global "
                          "tmpfs balloon - memory savings will be imprecise")
        for msg in blocking:
            (self.log.err if self.cfg["strict_preflight"] else self.log.warn)(msg)
        if blocking and self.cfg["strict_preflight"]:
            raise PreflightError(f"{len(blocking)} blocking preflight problem(s); fix the device or use --no-strict")

    def warmup(self) -> float:
        """Measure m_fg_i (foreground working set) and cold start latency for every app."""
        dev = self.dev
        self.log.head(f"warmup: cold-start {len(self.apps)} apps, dwell {self.cfg['warmup_dwell_s']}s each")
        for pkg in self.apps:
            dev.force_stop(pkg)
        time.sleep(2)
        t0 = time.time()
        for i, pkg in enumerate(self.apps):
            res = dev.launch(pkg)
            time.sleep(self.cfg["warmup_dwell_s"])
            s = dev.sample(self.apps)
            app = s["apps"][pkg]
            self.m_fg[pkg] = app.total("pss_kb") + app.total("swap_pss_kb")
            self.a_fg[pkg] = app.total("pss_anon_kb") + app.total("swap_pss_kb")
            fixed = self.cfg.get("fixed_m_fg_kb") or {}
            if pkg in fixed:                 # same denominator for every run of the matrix
                self.m_fg[pkg] = int(fixed[pkg])
                self.a_fg[pkg] = int((self.cfg.get("fixed_a_fg_kb") or {}).get(pkg, self.a_fg[pkg]))
            self.l_cold[pkg] = res.get("total_time_ms")
            self.emit({"type": "warmup", "pkg": pkg, "launch": res, "app": app.to_dict(),
                       "system": s["system"]})
            state = str(res.get("launch_state"))
            eta = (time.time() - t0) / (i + 1) * (len(self.apps) - i - 1)
            self.log.info(f"[warmup {i + 1:2d}/{len(self.apps)} ETA {fmt_dur(eta):>6}] {short_pkg(pkg, 22)} "
                          f"PSS {self.m_fg[pkg] // 1024:4d} MB (anon {self.a_fg[pkg] // 1024:4d}) "
                          f"cold {str(res.get('total_time_ms')):>5} ms [{state}]")
            if not app.alive or self.m_fg[pkg] == 0:
                self.log.warn(f"{pkg}: no live process after launch (crashed / killed?) - m_fg=0 will "
                              f"shrink the budget; consider removing it from the app list")
            elif state in ("FRONT", "TIMEOUT", "None"):
                self.log.warn(f"{pkg}: launch state {state} - onboarding screen or a very slow emulator; "
                              f"open it once by hand and dismiss dialogs")
        dev.home()   # otherwise the first request may already be in the foreground (FRONT)
        time.sleep(1)
        self.B_kb = self.cfg["eta"] * sum(self.m_fg.values())
        fixed = self.cfg.get("fixed_m_fg_kb") or {}
        source = "fixed" if fixed and all(p in fixed for p in self.apps) else ("mixed" if fixed else "measured")
        if fixed:
            missing = [p for p in self.apps if p not in fixed]
            if missing:
                self.log.warn(f"fixed_m_fg_kb has no entry for {missing}; those use this run's measurement")
        self.emit({"type": "budget", "eta": self.cfg["eta"], "budget_kb": self.B_kb, "m_fg_source": source,
                   "sum_m_fg_kb": sum(self.m_fg.values()), "m_fg_kb": self.m_fg, "a_fg_kb": self.a_fg})
        self.log.ok(f"budget B = {self.cfg['eta']} x {sum(self.m_fg.values()) // 1024} MB = {int(self.B_kb) // 1024} MB "
                    f"(m_fg {source})")
        return self.B_kb

    # ------------------------------------------------------------------ step
    def _infos(self, sample: dict, rho: float) -> dict[str, AppInfo]:
        infos = {}
        for pkg in self.apps:
            app = sample["apps"][pkg]
            if app.alive:
                m = (app.total("pss_kb") + app.total("swap_pss_kb")) * 1024
                a = (app.total("pss_anon_kb") + app.total("swap_pss_kb")) * 1024
            else:
                m, a = self.m_fg[pkg] * 1024, self.a_fg[pkg] * 1024
            # only /proc/<pid>/reclaim can restrict itself to anonymous pages; memcg reclaim
            # (v1 force_empty / v2 memory.reclaim) drops file pages too -> nothing stays behind
            anon_only = self.cfg["reclaim_mode"] == "anon" and \
                (self.dev.caps.reclaim_methods or ["balloon"])[0] == "proc_reclaim"
            b_keep = max(0.0, m - a) if anon_only else 0.0
            hist = self.resume_hist.get(pkg, [])
            comp = [ms for c, ms in hist if c and ms]
            hot = [ms for c, ms in hist if not c and ms]
            if comp and hot:
                c_resume = max(1.0, sum(comp) / len(comp) - sum(hot) / len(hot))
            else:
                c_resume = 60.0 + a / (1.2 * 1024 * 1024)   # ms, prior: 1.2 GB/s decompression
            infos[pkg] = AppInfo(pkg, m, a, rho, c_resume, b_keep)
        return infos

    def _zram_ratio(self, sample: dict) -> float:
        z = sample["system"].get("zram") or {}
        if z.get("orig_data_size", 0) > 64 * 1024 * 1024:
            return max(0.05, min(1.0, z["mem_used_total"] / z["orig_data_size"]))
        return self.cfg["default_rho"]

    def _apply(self, req: str, S: set[str], sample: dict, infos: dict[str, AppInfo]) -> dict:
        dev = self.dev
        actions = {"freeze": [], "unfreeze": [], "reclaim": {}, "balloon_mb": None}
        alive = {p for p in self.apps if sample["apps"][p].alive}
        need_balloon = False
        for pkg in self.apps:
            if pkg == req or pkg not in alive:
                continue
            resident = pkg in S
            want_frozen = (not resident) or self.cfg["freeze_resident"]
            if pkg in self.cfg["never_freeze"]:
                want_frozen = False
            if want_frozen and pkg not in self.frozen:
                actions["freeze"].append((pkg, dev.set_frozen(pkg, True)))
                self.frozen.add(pkg)
            elif not want_frozen and pkg in self.frozen:
                actions["unfreeze"].append((pkg, dev.set_frozen(pkg, False)))
                self.frozen.discard(pkg)
            if not resident and pkg not in self.compressed:
                how = dev.reclaim(pkg, self.cfg["reclaim_mode"])
                actions["reclaim"][pkg] = how
                if how == "balloon-needed":
                    need_balloon = True
                self.compressed.add(pkg)
            elif resident:
                self.compressed.discard(pkg)
        if need_balloon or dev._balloon_blocks:
            mi = sample["system"]["meminfo"]
            avail_mb = mi.get("MemAvailable", 0) // 1024
            if need_balloon:
                target = min(self.cfg["balloon_max_mb"],
                             max(0, avail_mb + dev._balloon_blocks * 64 - self.cfg["balloon_reserve_mb"]))
            else:
                target = 0
            actions["balloon_mb"] = dev.balloon_set(target)
        return actions

    def step(self, t: int, req: str) -> dict:
        B = self.B_kb * 1024
        pol = self.pol
        t_step = time.time()
        pol.on_request(t, req)
        was_compressed = req in self.compressed
        was_frozen = req in self.frozen
        if was_frozen:
            self.dev.set_frozen(req, False)
            self.frozen.discard(req)
        self.compressed.discard(req)
        launch = self.dev.launch(req)
        time.sleep(self.cfg["settle_s"])
        s1 = self.dev.sample(self.apps)
        rho = self._zram_ratio(s1)
        infos = self._infos(s1, rho)
        if launch.get("total_time_ms"):
            self.resume_hist.setdefault(req, []).append((was_compressed, launch["total_time_ms"]))
        t_dec = time.time()
        S = set(pol.choose_resident(t, req, infos, B, self.current)) | {req} | set(self.cfg["never_freeze"])
        decision_ms = (time.time() - t_dec) * 1000
        t_act = time.time()
        actions = self._apply(req, S, s1, infos)
        action_ms = (time.time() - t_act) * 1000
        self.current = S
        time.sleep(self.cfg["dwell_s"])
        s2 = self.dev.sample(self.apps)
        pids_now = {p: set(s2["apps"][p].to_dict()["pids"]) for p in self.apps}
        killed = [p for p in self.apps if self.prev_pids.get(p) and not pids_now[p]]
        kill_info = {}
        for p in killed:          # ask ActivityManager why (LOW_MEMORY? ANR? FREEZER? ...)
            try:
                ents = self.dev.exit_info(p, self.prev_pids.get(p))
                if ents:
                    e = ents[0]
                    kill_info[p] = {k: e.get(k) for k in ("timestamp", "pid", "reason", "subreason", "description")}
            except RESUMABLE_ERRORS:
                pass
        self.prev_pids = pids_now
        # measured background footprint: resident PSS + physical share of swapped-out pages
        M_bg = sum((s2["apps"][p].total("pss_kb") + rho * s2["apps"][p].total("swap_pss_kb")) * 1024
                   for p in self.apps if p != req)
        M_model = residency(S, self._infos(s2, rho), req)
        rec = {"type": "step", "t": t, "req": req, "launch": launch,
               "was_compressed": was_compressed, "was_frozen": was_frozen,
               "resident": sorted(S), "frozen": sorted(self.frozen),
               "compressed": sorted(self.compressed), "actions": actions,
               "rho_est": rho, "budget_kb": self.B_kb, "M_bg_kb": M_bg / 1024,
               "M_model_kb": M_model / 1024, "budget_violation": M_bg > B,
               "decision_ms": round(decision_ms, 3), "action_ms": round(action_ms, 1),
               "step_s": round(time.time() - t_step, 2), "killed_since_prev": killed, "kill_info": kill_info,
               "after_launch": {"system": s1["system"], "apps": {p: a.to_dict() for p, a in s1["apps"].items()}},
               "after_dwell": {"system": s2["system"], "apps": {p: a.to_dict() for p, a in s2["apps"].items()}},
               "tau_hat": pol.predicted_next_use(req)}
        self._log_step(rec, t_step)
        return rec

    def _log_step(self, rec: dict, t_step: float) -> None:
        T = len(self.trace)
        self.step_times.append(time.time() - t_step)
        recent = self.step_times[-10:]
        eta_s = sum(recent) / len(recent) * (T - rec["t"] - 1)
        launch = rec["launch"]
        state = str(launch.get("launch_state") or "?")
        ms = launch.get("total_time_ms")
        src = "zram" if rec["was_compressed"] else ("frz" if rec["was_frozen"] else "hot")
        acts = rec["actions"]
        act = f"frz+{len(acts['freeze'])}/-{len(acts['unfreeze'])} rcl {len(acts['reclaim'])}"
        if acts.get("balloon_mb") is not None:
            act += f" bal {acts['balloon_mb']}MB"
        mbg, b = int(rec["M_bg_kb"] / 1024), int(rec["budget_kb"] / 1024)
        viol = " VIOL" if rec["budget_violation"] else ""
        if rec["killed_since_prev"]:
            parts = []
            for k in rec["killed_since_prev"]:
                ki = (rec.get("kill_info") or {}).get(k) or {}
                why = ki.get("description") or ki.get("reason")
                parts.append(short_pkg(k) + (f"[{why}]" if why else ""))
            killed = " | killed: " + ",".join(parts)
        else:
            killed = ""
        line = (f"[{rec['t'] + 1:3d}/{T} {fmt_dur(time.time() - self.t_start):>6} ETA {fmt_dur(eta_s):>6}] "
                f"{short_pkg(rec['req'], 18)} {state:>7} {str(ms):>5} ms <-{src:<4} "
                f"| S={len(rec['resident']):2d} frz={len(rec['frozen']):2d} zram={len(rec['compressed']):2d} "
                f"| M_bg {mbg:4d}/{b} MB{viol} | {act} {rec['action_ms'] / 1000:.1f}s{killed}")
        (self.log.warn if rec["killed_since_prev"] else self.log.step)(line)

    # ------------------------------------------------------------------- run
    def run(self) -> str:
        path = self.open_log()
        T_cfg = self.cfg["trace"]["T"]
        if self.resume:
            state = self.load_checkpoint(path)
            if state is None:
                raise ResumeError(f"no checkpoint next to {path}; run without --resume")
            self._restore(state)
            self.log.head(f"resume {self.cfg['name']}: {self.t_done + 1}/{len(self.trace)} steps done, "
                          f"checkpoint from {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(state['saved_at']))}")
            self.prepare()
            self._reapply_device_state()
            self.emit({"type": "resume", "from_step": self.t_done + 1,
                       "checkpoint_saved_at": state["saved_at"]})
        else:
            self.log.head(f"run {self.cfg['name']}: policy={self.cfg['policy']} eta={self.cfg['eta']} "
                          f"T={T_cfg} trace={self.cfg['trace']['kind']} seed={self.cfg['trace']['seed']} -> {path}")
            try:
                self.prepare()
            except PreflightError:
                self.close()
                if os.path.exists(path) and os.path.getsize(path) == 0:
                    os.remove(path)          # nothing was measured; do not leave an empty result behind
                raise
            self.warmup()
            self.trace = make_trace(self.apps, self.cfg["trace"])
            self.emit({"type": "trace", "trace": self.trace})
            self.pol = make_policy(self.cfg["policy"], self.apps, **self.cfg["policy_kw"])
            self.current = set(self.apps)
            self.prev_pids = {p: set() for p in self.apps}
            self.save_checkpoint()
        self.t_start = time.time()
        self.step_times = []
        self.log.head(f"steps {self.t_done + 2}..{len(self.trace)}  (Ctrl+C is safe: re-run with --resume)")
        try:
            for t in range(self.t_done + 1, len(self.trace)):
                rec = self.step(t, self.trace[t])
                self.emit(rec)
                self.t_done = t
                self.save_checkpoint()
        except KeyboardInterrupt:
            self._abort("interrupted by user (Ctrl+C)")
            raise
        except RESUMABLE_ERRORS as e:
            self._abort(f"device/adb error: {type(e).__name__}: {str(e)[:200]}")
            raise
        except Exception as e:      # noqa: BLE001 - still leave a resumable checkpoint behind
            self._abort(f"unexpected error: {type(e).__name__}: {e}")
            raise
        self.cleanup()
        self.emit({"type": "end", "steps": self.t_done + 1, "wall_s": round(time.time() - self.t_start, 1)})
        self._remove_checkpoint()
        self._final_summary()
        self.close()
        return path

    def _abort(self, why: str) -> None:
        self.log.err(f"{why} after step {self.t_done + 1}/{len(self.trace)}")
        try:
            self.emit({"type": "interrupted", "after_step": self.t_done, "reason": why})
            self.cleanup()
        except Exception as e:    # noqa: BLE001 - device may be gone
            self.log.warn(f"cleanup on abort failed ({type(e).__name__}); device may still have frozen apps")
        self.log.warn(f"checkpoint kept: {self.ckpt_path}")
        self.log.warn(f"resume with:  python3 run_experiment.py <config> --resume {self.path}")
        self.close()

    def cleanup(self) -> None:
        for pkg in list(self.frozen):
            try:
                self.dev.set_frozen(pkg, False)
            except RESUMABLE_ERRORS:
                pass
        self.frozen.clear()
        if self.dev._balloon_blocks:
            self.dev.balloon_set(0)
        if self.cfg["stop_lmkd"]:
            self.dev.adb.shell("start lmkd")

    def _remove_checkpoint(self) -> None:
        try:
            os.remove(self.ckpt_path)
        except OSError:
            pass

    def close(self) -> None:
        if self.fh:
            self.fh.close()
            self.fh = None
        self.log.close()

    def _final_summary(self) -> None:
        try:
            from .analyze import load, summarize
            r = summarize(load(self.path))
        except Exception as e:    # noqa: BLE001 - summary is best effort
            self.log.warn(f"summary failed: {e}")
            return
        self.log.head(f"done {self.cfg['name']}: {r['T']} steps in {fmt_dur(time.time() - self.t_start)} -> {self.path}")
        self.log.ok(f"launches: HOT {r['n_hot']} WARM {r['n_warm']} COLD {r['n_cold']} "
                    f"TIMEOUT {r['n_timeout']} FRONT {r['n_front']} | processes killed by the system {r['n_killed']}"
                    + (f" ({r['kill_reasons']})" if r.get("kill_reasons") else ""))
        if r["n_killed"]:
            self.log.warn(f"{r['n_killed']} kills were not ours - each one turns a later resume into a COLD start. "
                          f"Reasons above come from `dumpsys activity exit-info`; see docs/02 §6 for what to do per reason")
        self.log.ok(f"resume latency P50/P95/P99 {r['lat_p50_ms']}/{r['lat_p95_ms']}/{r['lat_p99_ms']} ms "
                    f"(resident P50 {r['lat_resident_p50_ms']}, from zram P50 {r['lat_compressed_p50_ms']})")
        self.log.ok(f"background PSS avg {r['bg_pss_avg_mb']} MB (peak {r['bg_pss_peak_mb']}) vs budget "
                    f"{r['budget_mb']} MB, violations {r['budget_violations']}/{r['T']}, "
                    f"achieved eta {r.get('bg_pss_avg_over_sum_fg')}")
        self.log.ok(f"swap write/read {r['swap_write_mb']}/{r['swap_read_mb']} MB, refault anon {r['refault_anon']} "
                    f"file {r['refault_file']}, majfault {r['pgmajfault']}, reclaim via {r['reclaim_methods'] or '-'}")
        if self.log.warnings:
            self.log.warn(f"{len(self.log.warnings)} warning(s) during this run - see above / {self.path[:-6]}.log")


def load_config(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def run_from_config(cfg: dict, serial: Optional[str] = None, verbose: bool = False,
                    resume: bool = False, log=None) -> str:
    dev = Device(Adb(serial=serial, verbose=verbose))
    exp = Experiment(cfg, dev, log=log, resume=resume)
    return exp.run()
