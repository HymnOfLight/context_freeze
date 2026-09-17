"""In-memory stand-in for cf.device.Device so the runner and analyzer can be exercised
without an emulator.  Memory behaviour is a crude model: reclaim moves anon pages to
'swap' (SwapPss), a launch brings them back and costs latency proportional to the size."""
from __future__ import annotations

import random
import time

from cf.device import AppProc, AppSample, Caps, Device


class FakeAdb:
    adb = "fake-adb"
    serial = None
    verbose = False

    def shell(self, cmd, check=False, timeout=0):
        if "getprop ro.product.model" in cmd:
            return "sdk_gphone64_arm64\n"
        if "getprop ro.build.fingerprint" in cmd:
            return "google/sdk_gphone64_arm64/emu64a:fake\n"
        return ""

    def ensure_root(self):
        return "fake"

    def read_file(self, path):
        return ""

    def exists(self, path):
        return False


class FakeDevice(Device):
    def __init__(self, apps: list[str], seed: int = 0):
        super().__init__(FakeAdb())
        self.rng = random.Random(seed)
        self.pkgs = list(apps)
        self.state = {}
        for i, p in enumerate(apps):
            anon = self.rng.randint(120, 320) * 1024
            file = self.rng.randint(15, 45) * 1024
            self.state[p] = {"uid": 10100 + i, "pid": None, "anon": anon, "file": file,
                             "swap": 0, "frozen": False, "majflt": 0}
        self.vm = {"pswpin": 0, "pswpout": 0, "workingset_refault_anon": 0, "pgmajfault": 0}
        self.clock = 0.0

    def probe(self):
        self.caps = Caps(kernel="6.6.0-fake", android_sdk=35, cgroup_v2_root="/sys/fs/cgroup",
                         v2_controllers=["memory", "freezer"], freezer="cgroup.freeze",
                         reclaim_methods=["memcg_v2.memory.reclaim", "balloon"], zram=True,
                         mem_total_kb=self.mem_total_mb * 1024, cpu_count=4,
                         cached_apps_freezer=self.system_freezer)
        return self.caps

    def renderer(self):
        return self.gles

    def exit_info(self, pkg, pids=None):
        return [{"timestamp": "2026-01-01 00:00:00.000", "pid": next(iter(pids)) if pids else 0,
                 "reason": "ANR", "subreason": "UNKNOWN", "description": "bg anr"}] if self.kill_reason else []

    def uid_of(self, pkg):
        return self.state[pkg]["uid"] if pkg in self.state else None

    def launcher_activity(self, pkg):
        return f"{pkg}/.Main" if pkg in self.state else None

    def pids_of_uid(self, uid):
        return [(s["pid"], p) for p, s in self.state.items() if s["uid"] == uid and s["pid"]]

    def setup_zram(self, size_mb, algo=None):
        return "enabled"

    def force_stop(self, pkg):
        s = self.state[pkg]
        s["pid"], s["swap"], s["frozen"] = None, 0, False

    fail_at_launch: int | None = None     # raise AdbError on the n-th launch (crash injection)
    launches = 0
    mem_total_mb = 3 * 1024
    system_freezer = "disabled"           # preflight passes by default
    gles = "GLES: Apple, Apple M4, OpenGL ES 3.2"
    kill_reason = True
    kill_every: int | None = None         # kill the least recently launched bg app every n launches

    def launch(self, pkg):
        self.launches += 1
        if self.fail_at_launch is not None and self.launches == self.fail_at_launch:
            from cf.adb import AdbError
            raise AdbError("error: device 'emulator-5554' not found")
        s = self.state[pkg]
        if s["pid"] is None:
            s["pid"] = self.rng.randint(2000, 30000)
            state, ms = "COLD", 600 + s["anon"] // 400
        elif s["swap"] > 0:
            state, ms = "WARM", 150 + s["swap"] // 900
            self.vm["pswpin"] += s["swap"] // 4
            self.vm["workingset_refault_anon"] += s["swap"] // 8
            self.vm["pgmajfault"] += s["swap"] // 16
            s["majflt"] += s["swap"] // 16
            s["swap"] = 0
        else:
            state, ms = "HOT", 90 + self.rng.randint(0, 40)
        s["frozen"] = False
        if self.kill_every and self.launches % self.kill_every == 0:
            victims = [p for p, st in self.state.items() if st["pid"] and p != pkg]
            if victims:
                v = self.state[victims[0]]
                v["pid"], v["swap"], v["frozen"] = None, 0, False
        return {"status": "ok", "launch_state": state, "total_time_ms": ms, "wait_time_ms": ms + 10,
                "component": f"{pkg}/.Main", "host_elapsed_ms": ms + 30}

    def set_frozen(self, pkg, frozen):
        self.state[pkg]["frozen"] = frozen
        return "cgroup.freeze"

    def reclaim(self, pkg, mode="anon", bytes_hint=None):
        s = self.state[pkg]
        if s["pid"] is None:
            return "no-process"
        moved = int(s["anon"] * 0.92) - s["swap"]
        if moved > 0:
            s["swap"] += moved
            self.vm["pswpout"] += moved // 4
        return "memcg_v2.memory.reclaim"

    def sample(self, pkgs):
        self.clock += 1
        apps = {}
        swap_total = sum(s["swap"] for s in self.state.values())
        for p in pkgs:
            s = self.state[p]
            procs = []
            if s["pid"] is not None:
                resident_anon = s["anon"] - s["swap"]
                procs.append(AppProc(pid=s["pid"], name=p, pss_kb=resident_anon + s["file"],
                                     pss_anon_kb=resident_anon, pss_file_kb=s["file"],
                                     swap_pss_kb=s["swap"], rss_kb=resident_anon + s["file"] + 5000,
                                     majflt=s["majflt"], oom_score_adj=900, frozen=s["frozen"]))
            apps[p] = AppSample(pkg=p, uid=s["uid"], procs=procs)
        used = sum(s["anon"] - s["swap"] + s["file"] for s in self.state.values() if s["pid"])
        system = {
            "meminfo": {"MemTotal": 3 * 1024 * 1024, "MemAvailable": max(0, 2 * 1024 * 1024 - used)},
            "vmstat": dict(self.vm),
            "psi": {"some_total": self.clock * 1000, "full_total": self.clock * 100},
            "zram": {"orig_data_size": swap_total * 1024, "compr_data_size": int(swap_total * 1024 * 0.33),
                     "mem_used_total": int(swap_total * 1024 * 0.36)},
            "swaps": [{"filename": "/dev/block/zram0", "type": "partition", "size_kb": 1048572,
                       "used_kb": swap_total, "priority": 32767}],
        }
        return {"ts": time.time(), "system": system, "apps": apps}

    def balloon_setup(self, max_mb):
        self._balloon_blocks = 0

    def balloon_set(self, mb):
        self._balloon_blocks = mb // 64
        return mb
