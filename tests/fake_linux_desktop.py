"""In-memory stand-in for cf.linux.device.LinuxDesktop: lets the Ubuntu runner, checkpoint /
resume and analyzer be exercised without cgroups, X11 or a desktop.

Crude model of what the tiers do:
  * bg      the app's CPU is capped at `cpu_cores` -> throttled time accrues, progress slows
  * shrunk  bg + its resident memory drops to memory.high (the rest counts as swap); the next
            switch to it is WARM and costs latency proportional to what has to be paged back
Nothing disappears unless `crash_at_step` / `oom_app` inject it.
"""
from __future__ import annotations

import random
import time

from cf.linux.apps import LinuxApp
from cf.linux.cgroup import ThrottleProfile, tier_limits
from cf.linux.device import AppSampleL
from cf.linux.procs import ProcInfo

# `app_overrides` entries defining the fake apps (the runner rebuilds dev.cat from the config);
# cmd "true" exists everywhere, so cf.linux.apps.is_installed() accepts them.
OVERRIDES = {
    "fake_dl": {"name": "假下载", "category": "download"},
    "fake_browser": {"name": "假浏览器", "category": "download"},
    "fake_push": {"name": "假推送", "category": "push", "notify_names": ["假推送"]},
    "fake_chat": {"name": "假聊天", "category": "push"},
    "fake_office": {"name": "假办公", "category": "compute"},
    "fake_cad": {"name": "假CAD", "category": "compute"},
}
for _id, _ov in OVERRIDES.items():
    _ov.update({"cmd": "true", "match": _id, "wm_class": _id, "arch": ["any"]})

APPS = {k: LinuxApp(k, v["name"], v["category"], cmd="true", match=k, wm_class=k, arch=("any",),
                    notify_names=tuple(v.get("notify_names", ()))) for k, v in OVERRIDES.items()}


class FakeTree:
    def __init__(self):
        self.base = "/fake/cgroup/cf"
        self.leaves_: set[str] = set()

    def path(self, name=""):
        return f"{self.base}/{name}" if name else self.base

    def ensure(self, name):
        self.leaves_.add(name)
        return self.path(name)

    def exists(self, name=""):
        return name in self.leaves_ or name == ""

    def leaves(self):
        return sorted(self.leaves_)

    def resolve_cpuset(self, spec):
        return "3" if spec == "last" else spec


class FakeNotify:
    running = True
    error = ""

    def __init__(self):
        self.counts: dict[str, int] = {}

    def start(self):
        return True

    def snapshot(self):
        return dict(self.counts)

    def stop(self):
        pass


class FakeLinuxDesktop:
    cpu_wanted = {"download": 0.4, "push": 0.15, "compute": 2.5}     # cores the app would use unlimited

    def __init__(self, apps: list[str], seed: int = 0, cat: dict | None = None):
        self.rng = random.Random(seed)
        self.cat = dict(cat or APPS)
        self.tree = FakeTree()
        self.profile = ThrottleProfile()
        self.notify = FakeNotify()
        self.sudo_adopt = False
        self.launch_timeout = 90.0
        self.ping_timeout = 15.0
        self.tiers: dict[str, str] = {}
        self.caps: dict = {}
        self.clock = 0.0
        self.last_sample = 0.0
        self.active: str | None = None
        self.state: dict[str, dict] = {}
        for i, a in enumerate(apps):
            m = self.rng.randint(150, 450) * 1024          # kB
            self.state[a] = {"pid": None, "m": m, "resident": m, "high": None, "cpu_ms": 0, "throttled_ms": 0,
                             "nr_throttled": 0, "write": 0, "window": None, "majflt": 0, "oom": 0,
                             "notified": 0, "pid_age": 0.0}
        self.launches = 0
        self.fail_at_launch: int | None = None
        self.crash_app: str | None = None       # this app's process vanishes after the next sample
        self.oom_app: str | None = None         # memory.events oom_kill increments on the next sample
        self.vm = {"pswpin": 0, "pswpout": 0, "pgmajfault": 0}

    # ------------------------------------------------------------- probing
    def probe(self):
        self.caps = {"distro": "Ubuntu 22.04 (fake)", "kernel": "6.8.0-fake", "arch": "aarch64",
                     "mem_total_kb": 8 * 1024 * 1024, "swap_total_kb": 4 * 1024 * 1024, "cpu_count": 4,
                     "hypervisor": "Apple Virtualization", "cgroup_base": self.tree.base, "cgroup_available": True,
                     "cgroup_writable": True, "runner_inside_cf": True, "runner_cgroup": "0::/cf/host",
                     "controllers": ["cpuset", "cpu", "io", "memory"], "subtree_control": ["cpuset", "cpu", "io", "memory"],
                     "memory_reclaim": True, "host_leaf": True, "effective_cpus": [0, 1, 2, 3], "display": ":0",
                     "session_type": "x11", "wm": "Mutter", "x11_tools_missing": [],
                     "swaps": [{"filename": "/dev/zram0", "type": "partition", "size_kb": 4194300, "used_kb": 0,
                                "priority": 100}],
                     "zram": True, "ping_available": True, "ping_error": "", "notify_monitor": True, "notify_error": "",
                     "profile": self.profile.to_dict(), "apps": {}}
        return self.caps

    def caps_dict(self):
        return dict(self.caps)

    def adopt(self, id_):
        return {"moved": [], "failed": {}}

    # ------------------------------------------------------------- actions
    def launch(self, id_):
        self.launches += 1
        if self.fail_at_launch is not None and self.launches == self.fail_at_launch:
            raise OSError("X connection to :0 broken (explicit kill or server shutdown)")
        s = self.state[id_]
        res = {"status": "ok", "launch_state": None, "total_time_ms": None, "activate_ms": 40,
               "ping_ms": None, "launch_ms": None, "wid": None, "pid": None, "component": self.cat[id_].cmd}
        if s["pid"] is None:
            s["pid"] = self.rng.randint(2000, 30000)
            s["pid_age"] = 0.0
            s["window"] = 0x400000 + s["pid"]
            res["launch_state"] = "COLD"
            res["launch_ms"] = 400 + s["m"] // 500
            res["ping_ms"] = 5.0
        elif self.active == id_:
            res["launch_state"] = "FRONT"
            res["ping_ms"] = 0.3
        else:
            res["launch_state"] = "HOT"
            missing = s["m"] - s["resident"]
            res["ping_ms"] = round(0.3 + missing / 1200, 2)      # ~1.2 GB/s page-in
            if missing:
                self.vm["pswpin"] += missing // 4
                self.vm["pgmajfault"] += missing // 16
                s["majflt"] += missing // 16
            s["resident"] = s["m"]
        res["wid"], res["pid"] = s["window"], s["pid"]
        if res["launch_state"] != "FRONT":
            res["total_time_ms"] = int((res["launch_ms"] or 0) + res["activate_ms"] + res["ping_ms"])
        res["host_elapsed_ms"] = (res["total_time_ms"] or 0) + 10
        self.active = id_
        self.tiers[id_] = "fg"
        s["high"] = None
        return res

    def minimize(self, id_):
        pass

    def set_tier(self, id_, tier, m_fg_bytes=None):
        s = self.state[id_]
        lim = tier_limits(self.tree, self.profile, tier, m_fg_bytes)
        self.tiers[id_] = tier
        res = {"tier": tier, "limits": lim, "reclaim": None, "reclaim_bytes": 0}
        if tier == "shrunk":
            high_kb = int(lim["memory_high"]) // 1024
            s["high"] = high_kb
            if s["resident"] > high_kb:
                moved = s["resident"] - high_kb
                self.vm["pswpout"] += moved // 4
                s["resident"] = high_kb
                res["reclaim"], res["reclaim_bytes"] = "memory.reclaim", moved * 1024
            else:
                res["reclaim"] = "nothing"
        else:
            s["high"] = None
        return res

    def release_all(self):
        n = 0
        for a in self.state:
            self.tiers[a] = "fg"
            self.state[a]["high"] = None
            n += 1
        return n

    # ------------------------------------------------------------ sampling
    def _advance(self, dt: float):
        for a, s in self.state.items():
            if s["pid"] is None:
                continue
            s["pid_age"] += dt
            cat = self.cat[a].category
            want = self.cpu_wanted[cat] if a != self.active else 1.0
            tier = self.tiers.get(a, "fg")
            cap = self.profile.cpu_cores if tier in ("bg", "shrunk") else 4.0
            got = min(want, cap)
            s["cpu_ms"] += int(got * dt * 1000)
            if want > cap:
                s["throttled_ms"] += int((want - cap) / want * dt * 1000)
                s["nr_throttled"] += int(dt * 10)
            if cat == "download":
                s["write"] += int(2 * 2 ** 20 * dt * got / want)
            if cat == "push" and self.rng.random() < min(1.0, 5 * dt):
                s["notified"] += 1
                self.notify.counts[self.cat[a].name] = self.notify.counts.get(self.cat[a].name, 0) + 1

    def sample(self, ids, with_windows=True):
        # progress follows real wall time so that rates (cores, MB/s) computed by the runner are sane
        now = time.monotonic()
        dt = min(5.0, now - self.last_sample) if self.last_sample else 0.0
        self.last_sample = now
        self.clock += dt
        self._advance(dt)
        apps = {}
        for a in ids:
            s = self.state[a]
            procs = []
            if s["pid"] is not None:
                swap = s["m"] - s["resident"]
                procs.append(ProcInfo(pid=s["pid"], name=a, cmdline=self.cat[a].cmd, pss_kb=s["resident"],
                                      pss_anon_kb=int(s["resident"] * 0.8), pss_file_kb=int(s["resident"] * 0.2),
                                      swap_pss_kb=swap, rss_kb=s["resident"] + 4000, majflt=s["majflt"],
                                      cpu_ms=s["cpu_ms"], write_bytes=s["write"], state="S", threads=8,
                                      age_s=s["pid_age"]))
            cg = {"memory_current": s["resident"] * 1024, "swap_current": (s["m"] - s["resident"]) * 1024,
                  "memory_high": str(s["high"] * 1024) if s["high"] else "max",
                  "cpu": {"usage_usec": s["cpu_ms"] * 1000, "throttled_usec": s["throttled_ms"] * 1000,
                          "nr_throttled": s["nr_throttled"]},
                  "events": {"low": 0, "high": 0, "max": 0, "oom": s["oom"], "oom_kill": s["oom"]}}
            apps[a] = AppSampleL(app=a, procs=procs, cgroup=cg, window=s["window"] if procs else None,
                                 tier=self.tiers.get(a, "fg"))
        if self.crash_app and self.state[self.crash_app]["pid"]:
            self.state[self.crash_app].update(pid=None, window=None, resident=self.state[self.crash_app]["m"])
            self.crash_app = None
        if self.oom_app:
            self.state[self.oom_app]["oom"] += 1
            self.oom_app = None
        used = sum(s["resident"] for s in self.state.values() if s["pid"])
        system = {"meminfo": {"MemTotal": 8 * 1024 * 1024, "MemAvailable": max(0, 6 * 1024 * 1024 - used)},
                  "vmstat": dict(self.vm), "psi": {"some_total": int(self.clock * 1000), "full_total": int(self.clock * 100)},
                  "psi_cpu": {"some_total": int(self.clock * 500)}, "loadavg": [1.0, 1.0, 1.0],
                  "zram": {"orig_data_size": 0, "compr_data_size": 0, "mem_used_total": 0},
                  "swaps": self.caps.get("swaps", [])}
        return {"ts": time.time(), "system": system, "apps": apps, "notifications": self.notify.snapshot()}

    @staticmethod
    def state_check(before, after):
        from cf.linux.device import LinuxDesktop
        return LinuxDesktop.state_check(before, after)

    def notifications_for(self, id_, before, after):
        from cf.linux.notify import NotifyCounter
        a = self.cat[id_]
        return NotifyCounter.count_for(NotifyCounter.delta(before, after), list(a.notify_names) + [a.name, a.id])

    def close(self):
        pass
