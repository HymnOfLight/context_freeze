"""On-device experiment loop (Android emulator or rooted phone).

For every request r_t in the trace:
  1. unfreeze r_t, `am start -W` it (records resume latency + HOT/WARM/COLD)
  2. sample per-app memory (PSS anon/file, SwapPss) and system counters
  3. ask the policy for the resident set S_t under the budget B = eta * sum_i m_fg_i
  4. freeze every background app outside S_t and reclaim its anonymous pages
     (memcg memory.reclaim -> /proc/<pid>/reclaim -> tmpfs balloon fallback)
  5. dwell, sample again, append one JSON line to the result file
"""
from __future__ import annotations

import json
import os
import time
from typing import Optional

from .adb import Adb
from .device import Device
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
}


class Experiment:
    def __init__(self, cfg: dict, dev: Device, log=print):
        self.cfg = {**DEFAULTS, **cfg}
        self.cfg["trace"] = {**DEFAULTS["trace"], **cfg.get("trace", {})}
        self.dev = dev
        self.log = log
        self.apps: list[str] = []
        self.m_fg: dict[str, int] = {}      # kB, foreground reference working set
        self.a_fg: dict[str, int] = {}      # kB, anon part
        self.l_cold: dict[str, Optional[int]] = {}
        self.frozen: set[str] = set()
        self.compressed: set[str] = set()
        self.resume_hist: dict[str, list[tuple[bool, int]]] = {}   # (was_compressed, ms)
        self.fh = None

    # ------------------------------------------------------------- lifecycle
    def open_log(self) -> str:
        os.makedirs(self.cfg["out_dir"], exist_ok=True)
        name = self.cfg.get("name") or f"{self.cfg['policy']}_eta{self.cfg['eta']}_{int(time.time())}"
        path = os.path.join(self.cfg["out_dir"], name + ".jsonl")
        self.fh = open(path, "w")
        return path

    def emit(self, rec: dict) -> None:
        rec.setdefault("ts", time.time())
        self.fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.fh.flush()

    def prepare(self) -> None:
        dev, a = self.dev, self.dev.adb
        root = a.ensure_root()
        caps = dev.probe()
        self.log(f"root via {root}; kernel {caps.kernel}; sdk {caps.android_sdk}; "
                 f"freezer={caps.freezer}; reclaim={caps.reclaim_methods}; zram={caps.zram}; "
                 f"swaps={[s['filename'] for s in caps.swaps]}; cached_apps_freezer={caps.cached_apps_freezer!r}")
        if self.cfg["zram_mb"]:
            self.log("zram: " + dev.setup_zram(self.cfg["zram_mb"], self.cfg.get("zram_algo")))
        if self.cfg["swapfile_mb"]:
            self.log("swapfile: " + dev.setup_swapfile(self.cfg["swapfile_mb"]))
        dev.probe()
        if self.cfg["stop_lmkd"]:
            a.shell("stop lmkd")
            self.log("lmkd stopped (kernel OOM killer is the only safety net now)")
        a.shell("svc power stayon true; input keyevent KEYCODE_WAKEUP; wm dismiss-keyguard")
        for k in ("window_animation_scale", "transition_animation_scale", "animator_duration_scale"):
            a.shell(f"settings put global {k} 0")
        # validate app list
        self.apps = []
        for pkg in self.cfg["apps"]:
            uid = dev.uid_of(pkg)
            if dev.launcher_activity(pkg) and uid is not None:
                self.apps.append(pkg)
                if not Device.is_isolated_uid(uid) and pkg not in self.cfg["never_freeze"]:
                    # e.g. Settings shares uid 1000 with system_server: observe only
                    self.log(f"WARN: {pkg} runs under shared system uid {uid}; added to never_freeze")
                    self.cfg["never_freeze"] = list(self.cfg["never_freeze"]) + [pkg]
            else:
                self.log(f"WARN: {pkg} not installed / no launcher activity - dropped")
        if len(self.apps) < 2:
            raise RuntimeError("need at least two launchable apps")
        if "balloon" in caps.reclaim_methods and len(caps.reclaim_methods) == 1:
            dev.balloon_setup(self.cfg["balloon_max_mb"])
        self.emit({"type": "meta", "config": self.cfg, "apps": self.apps, "caps": dev.caps_dict(),
                   "device": {"model": a.shell("getprop ro.product.model").strip(),
                              "build": a.shell("getprop ro.build.fingerprint").strip()}})

    def warmup(self) -> float:
        """Measure m_fg_i (foreground working set) and cold start latency for every app."""
        dev = self.dev
        for pkg in self.apps:
            dev.force_stop(pkg)
        time.sleep(2)
        for pkg in self.apps:
            res = dev.launch(pkg)
            time.sleep(self.cfg["warmup_dwell_s"])
            s = dev.sample(self.apps)
            app = s["apps"][pkg]
            self.m_fg[pkg] = app.total("pss_kb") + app.total("swap_pss_kb")
            self.a_fg[pkg] = app.total("pss_anon_kb") + app.total("swap_pss_kb")
            self.l_cold[pkg] = res.get("total_time_ms")
            self.emit({"type": "warmup", "pkg": pkg, "launch": res, "app": app.to_dict(),
                       "system": s["system"]})
            self.log(f"warmup {pkg}: PSS {self.m_fg[pkg]//1024} MB (anon {self.a_fg[pkg]//1024} MB) "
                     f"cold {res.get('total_time_ms')} ms [{res.get('launch_state')}]")
        dev.home()   # otherwise the first request may already be in the foreground (FRONT)
        time.sleep(1)
        B_kb = self.cfg["eta"] * sum(self.m_fg.values())
        self.emit({"type": "budget", "eta": self.cfg["eta"], "budget_kb": B_kb,
                   "sum_m_fg_kb": sum(self.m_fg.values()), "m_fg_kb": self.m_fg, "a_fg_kb": self.a_fg})
        self.log(f"budget B = {self.cfg['eta']} * {sum(self.m_fg.values())//1024} MB = {int(B_kb)//1024} MB")
        return B_kb

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

    def run(self) -> str:
        path = self.open_log()
        self.prepare()
        B_kb = self.warmup()
        B = B_kb * 1024
        trace = make_trace(self.apps, self.cfg["trace"])
        self.emit({"type": "trace", "trace": trace})
        pol = make_policy(self.cfg["policy"], self.apps, **self.cfg["policy_kw"])
        current = set(self.apps)
        prev_pids = {p: set() for p in self.apps}
        for t, req in enumerate(trace):
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
            S = set(pol.choose_resident(t, req, infos, B, current)) | {req} | set(self.cfg["never_freeze"])
            decision_ms = (time.time() - t_dec) * 1000
            t_act = time.time()
            actions = self._apply(req, S, s1, infos)
            action_ms = (time.time() - t_act) * 1000
            current = S
            time.sleep(self.cfg["dwell_s"])
            s2 = self.dev.sample(self.apps)
            pids_now = {p: set(s2["apps"][p].to_dict()["pids"]) for p in self.apps}
            killed = [p for p in self.apps if prev_pids[p] and not pids_now[p]]
            prev_pids = pids_now
            # measured background footprint: resident PSS + physical share of swapped-out pages
            M_bg = sum((s2["apps"][p].total("pss_kb") + rho * s2["apps"][p].total("swap_pss_kb")) * 1024
                       for p in self.apps if p != req)
            M_model = residency(S, self._infos(s2, rho), req)
            rec = {"type": "step", "t": t, "req": req, "launch": launch,
                   "was_compressed": was_compressed, "was_frozen": was_frozen,
                   "resident": sorted(S), "frozen": sorted(self.frozen),
                   "compressed": sorted(self.compressed), "actions": actions,
                   "rho_est": rho, "budget_kb": B_kb, "M_bg_kb": M_bg / 1024,
                   "M_model_kb": M_model / 1024, "budget_violation": M_bg > B,
                   "decision_ms": round(decision_ms, 3), "action_ms": round(action_ms, 1),
                   "step_s": round(time.time() - t_step, 2), "killed_since_prev": killed,
                   "after_launch": {"system": s1["system"], "apps": {p: a.to_dict() for p, a in s1["apps"].items()}},
                   "after_dwell": {"system": s2["system"], "apps": {p: a.to_dict() for p, a in s2["apps"].items()}},
                   "tau_hat": pol.predicted_next_use(req)}
            self.emit(rec)
            self.log(f"[{t:3d}] {req:40s} {str(launch.get('launch_state')):>5} {launch.get('total_time_ms')} ms "
                     f"| resident={len(S)} frozen={len(self.frozen)} compressed={len(self.compressed)} "
                     f"| M_bg={int(M_bg/2**20)}MB B={int(B/2**20)}MB {'VIOL' if M_bg > B else ''}")
        self.cleanup()
        return path

    def cleanup(self) -> None:
        for pkg in list(self.frozen):
            self.dev.set_frozen(pkg, False)
        self.frozen.clear()
        if self.dev._balloon_blocks:
            self.dev.balloon_set(0)
        if self.cfg["stop_lmkd"]:
            self.dev.adb.shell("start lmkd")
        self.emit({"type": "end"})
        if self.fh:
            self.fh.close()


def load_config(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def run_from_config(cfg: dict, serial: Optional[str] = None, verbose: bool = False) -> str:
    dev = Device(Adb(serial=serial, verbose=verbose))
    exp = Experiment(cfg, dev)
    try:
        return exp.run()
    except KeyboardInterrupt:
        exp.log("interrupted - unfreezing everything")
        exp.cleanup()
        raise
