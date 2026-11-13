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
# bench apps (kind "bench", cf.linux.bench_app on the real desktop): a compute one logging jobs and
# a download one logging MB per interval + connect times - add them to a test's apps / app_overrides
BENCH_OVERRIDES = {
    "fake_num": {"name": "假计算基准", "category": "compute", "kind": "bench"},
    "fake_dlb": {"name": "假下载基准", "category": "download", "kind": "bench"},
}
for _id, _ov in {**OVERRIDES, **BENCH_OVERRIDES}.items():
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
        self.mem_peak: dict[str, int] = {}      # app -> memory.peak of its cgroup, kB
        self.profile = ThrottleProfile()
        self.notify = FakeNotify()
        self.mobile = None                      # no Android runtime (subclass / set to fake one)
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
                             "notified": 0, "pid_age": 0.0, "high_events": 0, "pgsteal": 0,
                             # what memory.reclaim can never take away (pinned / hot pages): 15 % of m
                             "floor": m * 15 // 100}
        # apps whose background working set (kB) is bigger than any memory.high the profile gives them:
        # while shrunk below it they thrash (memory.events high / major faults keep growing)
        self.bg_wss: dict[str, int] = {}
        self.launches = 0
        self.fail_at_launch: int | None = None
        self.crash_app: str | None = None       # this app's process vanishes after the next sample
        self.oom_app: str | None = None         # memory.events oom_kill increments on the next sample
        self.vm = {"pswpin": 0, "pswpout": 0, "pgmajfault": 0}
        self.ping_dead: set[str] = set()        # apps that never answer _NET_WM_PING (no smooth probe)
        self.bench_job_s = 0.004                # a bench job with a whole core; slower by 1 / cpu_cores when limited
        self.bench_mbps = 40.0                  # download speed with a whole core
        self.bench_tick_s = 0.01                # download: one "dl" + one "rtt" record per tick
        # bench apps driving a real tool with several operations per cycle (7-Zip: compress, extract):
        # app -> [(op, duration as a multiple of bench_job_s, cores it uses)]
        self.bench_ops: dict[str, list[tuple[str, float, float]]] = {}
        # real download apps watched through their sockets (cf.linux.netstat on the real desktop)
        self.net_apps: list[str] = []
        self.net_mbps = 3.0                     # what such an app downloads with a whole core
        self.net_calls: list[dict] = []
        self._bench_t: dict[str, float] = {}
        self._woke_missing: dict[str, int] = {}

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

    def is_installed(self, a: LinuxApp) -> bool:
        from cf.linux.apps import is_installed
        return is_installed(a, mobile=self.mobile)

    def adopt(self, id_):
        return {"moved": [], "failed": {}}

    def mem_peak_kb(self, id_):
        return self.mem_peak.get(id_)

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
            self._woke_missing[id_] = missing
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

    def smooth_probe(self, id_, wid, n=5, every_s=0.2, timeout_s=1.0):
        """The first ping after a switch back from a shrunk tier is slow (pages still coming back)."""
        if id_ in self.ping_dead or not wid or n <= 0:
            return {"supported": False, "pings_ms": []}
        first = round(0.3 + self._woke_missing.pop(id_, 0) / 500, 2)
        return {"supported": True, "pings_ms": [first] + [0.3] * (n - 1)}

    def net_start(self, ids, every_s=1.0, loopback=False, by="auto", strays=False, probe_host="", probe_every_s=0.5):
        self.net_apps = list(ids)
        self.net_probe_host = probe_host
        self.net_calls.append({"ids": list(ids), "every_s": every_s, "loopback": loopback, "by": by,
                               "probe_host": probe_host})

    def net_stop(self):
        self.net_apps = []

    def bench_read(self, id_):
        """Records a bench app would have logged since the last call, at the speed its current tier
        allows: compute -> "job" records, download -> "dl" + "rtt" records; for a real download app
        watched with net_start(): what the socket sampler would have seen."""
        s = self.state[id_]
        now = time.time()
        net = id_ in self.net_apps and self.cat[id_].kind != "bench"
        if s["pid"] is None or not (net or self.cat[id_].kind == "bench"):
            return []
        t = self._bench_t.get(id_, now)
        limited = self.tiers.get(id_, "fg") in ("bg", "shrunk")
        share = min(1.0, self.profile.cpu_cores) if limited else 1.0
        out = []
        if net:
            # a limited app reads its sockets late: less arrives, and what arrived waits in the kernel
            while t + self.bench_tick_s <= now:
                t += self.bench_tick_s
                out.append({"t": t, "k": "dl", "b": int(self.net_mbps * share * 2 ** 20 * self.bench_tick_s),
                            "s": self.bench_tick_s, "tx": 400, "n": 4, "q": 900_000 if limited else 20_000, "retr": 0})
                out.append({"t": t, "k": "rtt", "ms": 23.5, "to": "dl", "n": 3})
                if getattr(self, "net_probe_host", ""):           # the probe inside the app's cgroup
                    late = round(10 * (1 - share), 2) if limited else 0.0
                    out.append({"t": t, "k": "rtt", "ms": round(31.0 + late, 2), "to": "ext", "late": late})
        elif id_ in self.bench_ops:
            ops = self.bench_ops[id_]
            i = self._bench_i = getattr(self, "_bench_i", {})
            while True:
                op, mult, cores = ops[i.get(id_, 0) % len(ops)]
                d = self.bench_job_s * mult / share
                if t + d > now:
                    break
                t += d
                i[id_] = i.get(id_, 0) + 1
                out.append({"t": t, "k": "job", "op": op, "ms": round(d * 1000, 3),
                            "cpu": round(self.bench_job_s * mult * cores * 1000, 3), "w": 0, "ok": True})
        elif self.cat[id_].category == "download":
            while t + self.bench_tick_s <= now:
                t += self.bench_tick_s
                out.append({"t": t, "k": "dl", "b": int(self.bench_mbps * share * 2 ** 20 * self.bench_tick_s),
                            "s": self.bench_tick_s})
                out.append({"t": t, "k": "rtt", "ms": 0.4, "to": "dl", "late": round(10 * (1 - share), 2) if limited else 0.0})
        else:
            d = self.bench_job_s / share
            while t + d <= now:
                t += d
                out.append({"t": t, "k": "job", "ms": round(d * 1000, 3), "cpu": round(self.bench_job_s * 1000, 3),
                            "w": 0, "ok": True})
        self._bench_t[id_] = t
        return out

    def set_tier(self, id_, tier, m_fg_bytes=None, memory_high_bytes=None):
        s = self.state[id_]
        lim = tier_limits(self.tree, self.profile, tier, m_fg_bytes, memory_high_bytes)
        self.tiers[id_] = tier
        res = {"tier": tier, "limits": lim, "reclaim": None, "reclaim_bytes": 0}
        if tier == "shrunk":
            high_kb = int(lim["memory_high"]) // 1024
            s["high"] = high_kb
            if self.profile.squeeze_rounds > 0:
                sq = self.squeeze(id_)
                res["reclaim"], res["reclaim_bytes"], res["squeeze"] = sq["how"], sq["asked_bytes"], sq
            elif s["resident"] > high_kb:
                moved = s["resident"] - high_kb
                self._reclaimed(s, moved)
                res["reclaim"], res["reclaim_bytes"] = "memory.reclaim", moved * 1024
            else:
                res["reclaim"] = "nothing"
        else:
            s["high"] = None
        return res

    def _reclaimed(self, s, moved_kb):
        self.vm["pswpout"] += moved_kb // 4
        s["pgsteal"] += moved_kb // 4
        s["resident"] -= moved_kb

    def squeeze(self, id_, rounds=None):
        """memory.reclaim of the whole cgroup: everything above the pinned floor goes to swap."""
        s = self.state[id_]
        rounds = self.profile.squeeze_rounds if rounds is None else rounds
        before = s["resident"] * 1024
        asked = 0
        for _ in range(rounds):
            asked += s["resident"] * 1024
            moved = max(0, s["resident"] - s["floor"])
            self._reclaimed(s, moved)
        self.squeezes = getattr(self, "squeezes", 0) + 1
        return {"how": f"memory.reclaim x{rounds}", "rounds": rounds, "asked_bytes": asked,
                "before_bytes": before, "after_bytes": s["resident"] * 1024}

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
            wss = self.bg_wss.get(a)
            if wss and tier == "shrunk" and s["high"] and s["high"] < wss:
                s["high_events"] += dt * 40                  # the kernel reclaims it ~40x per second
                s["majflt"] += int(dt * 2000)
                self.vm["pswpout"] += int(dt * 2000); self.vm["pswpin"] += int(dt * 2000)
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
                # a main process holding 90 % and a helper (renderer / utility) holding 10 %, both swapping alike
                for pid, name, share in ((s["pid"], a, 0.9), (s["pid"] + 1, a + "-helper", 0.1)):
                    res_kb, swap_kb = int(s["resident"] * share), int(swap * share)
                    procs.append(ProcInfo(pid=pid, name=name, cmdline=self.cat[a].cmd, pss_kb=res_kb,
                                          pss_anon_kb=int(res_kb * 0.8), pss_file_kb=int(res_kb * 0.2),
                                          swap_pss_kb=swap_kb, rss_kb=res_kb + 4000, majflt=int(s["majflt"] * share),
                                          cpu_ms=int(s["cpu_ms"] * share), write_bytes=int(s["write"] * share),
                                          state="S", threads=8, age_s=s["pid_age"]))
            cg = {"memory_current": s["resident"] * 1024, "swap_current": (s["m"] - s["resident"]) * 1024,
                  "memory_high": str(s["high"] * 1024) if s["high"] else "max",
                  "cpu": {"usage_usec": s["cpu_ms"] * 1000, "throttled_usec": s["throttled_ms"] * 1000,
                          "nr_throttled": s["nr_throttled"]},
                  "events": {"low": 0, "high": int(s["high_events"]), "max": 0, "oom": s["oom"], "oom_kill": s["oom"]},
                  "pgsteal": s["pgsteal"], "pgscan": s["pgsteal"]}
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
        self.net_stop()
        pass


class FakeWorkDriver:
    """Stand-in for cf.linux.work.WorkDriver: each app has a "document" that scripted work appends
    to; the oracle is the document itself.  `lose(app)` empties it (as a relaunch / reset would),
    so the next return of that app is reported as work_state_ok=False."""

    def __init__(self, dev, cfg=None):
        self.dev = dev
        self.cfg = cfg or {}
        self.docs: dict[str, str] = {}
        self.calls: list[tuple[str, str, int]] = []
        self.lost_pending: set[str] = set()

    def spec(self, a):
        from cf.linux.work import default_spec
        s = default_spec(a)
        s.update((self.cfg.get("work_overrides") or {}).get(a.id) or {})
        return s

    def work(self, app_id, wid, seconds, t):
        from cf.linux.work import work_text
        self.calls.append((app_id, "work", t))
        self.docs[app_id] = self.docs.get(app_id, "") + work_text(app_id, t)
        return {"kind": self.spec(self.dev.cat[app_id])["kind"], "status": "ok", "elapsed_ms": 1, "oracle": "clipboard"}

    def oracle(self, app_id, wid):
        self.calls.append((app_id, "oracle", -1))
        if app_id in self.lost_pending:
            self.lost_pending.discard(app_id)
            self.docs[app_id] = ""
        return {"oracle": "clipboard", "value": self.docs.get(app_id, ""), "ts": time.time(), "read_ms": 180}

    def compare(self, before, after, app_id=None):
        from cf.linux.work import compare
        return compare(before, after)

    def lose(self, app_id):
        self.lost_pending.add(app_id)

    def describe(self, apps, suffix=""):
        return "\n".join(f"{p} fake{suffix}" for p in apps)
