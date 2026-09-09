"""Device-side primitives: capability probing, metric sampling, freeze / reclaim / balloon.

All knobs are probed at runtime because emulator images differ (cgroup v1 vs v2 memcg,
presence of /proc/<pid>/reclaim, zram availability, ...).  Every action reports which
mechanism was actually used so the experiment log stays honest.
"""
from __future__ import annotations

import re
import shlex
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

from .adb import Adb
from . import parsers as P

BALLOON_DIR = "/data/local/tmp/cf_balloon"
BALLOON_BLOCK_MB = 64


@dataclass
class Caps:
    kernel: str = ""
    android_sdk: int = 0
    page_size: int = 4096
    cgroup_v2_root: Optional[str] = None      # e.g. /sys/fs/cgroup
    v2_controllers: list[str] = field(default_factory=list)
    freezer: Optional[str] = None             # 'cgroup.freeze' | 'sigstop'
    memcg_v1_root: Optional[str] = None       # e.g. /dev/memcg
    reclaim_methods: list[str] = field(default_factory=list)  # ordered preference
    zram: bool = False
    swaps: list[dict] = field(default_factory=list)
    cached_apps_freezer: str = ""
    cpu_count: int = 0
    mem_total_kb: int = 0


@dataclass
class AppProc:
    pid: int
    name: str
    pss_kb: int = 0
    pss_anon_kb: int = 0
    pss_file_kb: int = 0
    pss_shmem_kb: int = 0
    swap_pss_kb: int = 0
    rss_kb: int = 0
    majflt: int = 0
    oom_score_adj: Optional[int] = None
    frozen: Optional[bool] = None


@dataclass
class AppSample:
    pkg: str
    uid: int
    procs: list[AppProc]

    @property
    def alive(self) -> bool:
        return bool(self.procs)

    def total(self, attr: str) -> int:
        return sum(getattr(p, attr) or 0 for p in self.procs)

    def to_dict(self) -> dict:
        return {"pkg": self.pkg, "uid": self.uid, "alive": self.alive,
                "pss_kb": self.total("pss_kb"), "pss_anon_kb": self.total("pss_anon_kb"),
                "pss_file_kb": self.total("pss_file_kb"), "pss_shmem_kb": self.total("pss_shmem_kb"),
                "swap_pss_kb": self.total("swap_pss_kb"), "rss_kb": self.total("rss_kb"),
                "majflt": self.total("majflt"),
                "frozen": any(p.frozen for p in self.procs) if self.procs else None,
                "pids": [p.pid for p in self.procs],
                "oom_score_adj": [p.oom_score_adj for p in self.procs]}


class Device:
    def __init__(self, adb: Adb):
        self.adb = adb
        self.caps = Caps()
        self._uid_cache: dict[str, int] = {}
        self._activity_cache: dict[str, str] = {}
        self._balloon_blocks = 0

    # ------------------------------------------------------------------ probing
    def probe(self) -> Caps:
        a = self.adb
        c = self.caps
        c.kernel = a.shell("uname -r").strip()
        try:
            c.android_sdk = int(a.shell("getprop ro.build.version.sdk").strip() or 0)
        except ValueError:
            c.android_sdk = 0
        try:
            c.page_size = int(a.shell("getconf PAGESIZE").strip() or 4096)
        except ValueError:
            c.page_size = 4096
        c.cpu_count = len(P.parse_pid_list(a.shell("ls /sys/devices/system/cpu | grep -E '^cpu[0-9]+$' | sed 's/cpu//'")))
        c.mem_total_kb = P.parse_kv_kb(a.read_file("/proc/meminfo")).get("MemTotal", 0)

        if a.exists("/sys/fs/cgroup/cgroup.controllers"):
            c.cgroup_v2_root = "/sys/fs/cgroup"
            c.v2_controllers = a.read_file("/sys/fs/cgroup/cgroup.controllers").split()
        if a.exists("/dev/memcg/memory.limit_in_bytes") or a.exists("/dev/memcg/apps"):
            c.memcg_v1_root = "/dev/memcg"

        # freezer: Android >= 11 mounts cgroup v2 with the freezer at /sys/fs/cgroup/uid_*/pid_*
        if c.cgroup_v2_root and "uid_" in a.shell(f"ls {c.cgroup_v2_root} | head -50"):
            c.freezer = "cgroup.freeze"
        else:
            c.freezer = "sigstop"

        methods = []
        if c.cgroup_v2_root and "memory" in c.v2_controllers:
            methods.append("memcg_v2.memory.reclaim")
        if c.memcg_v1_root:
            methods.append("memcg_v1.force_empty")
        if a.exists("/proc/self/reclaim"):
            methods.append("proc_reclaim")
        methods.append("balloon")
        c.reclaim_methods = methods

        c.zram = a.exists("/sys/block/zram0")
        c.swaps = P.parse_swaps(a.read_file("/proc/swaps"))
        c.cached_apps_freezer = a.shell("settings get global cached_apps_freezer").strip()
        return c

    # ------------------------------------------------------------- app identity
    def uid_of(self, pkg: str) -> Optional[int]:
        if pkg in self._uid_cache:
            return self._uid_cache[pkg]
        out = self.adb.shell(f"cmd package list packages -U {shlex.quote(pkg)}")
        m = re.search(rf"package:{re.escape(pkg)}\s+uid:(\d+)", out)
        uid = int(m.group(1)) if m else P.parse_uid_from_dumpsys(
            self.adb.shell(f"dumpsys package {shlex.quote(pkg)} | grep -m1 userId="))
        if uid is not None:
            self._uid_cache[pkg] = uid
        return uid

    def launcher_activity(self, pkg: str) -> Optional[str]:
        if pkg in self._activity_cache:
            return self._activity_cache[pkg]
        out = self.adb.shell(
            f"cmd package resolve-activity --brief -a android.intent.action.MAIN "
            f"-c android.intent.category.LAUNCHER {shlex.quote(pkg)}")
        comp = P.parse_resolve_activity(out)
        if comp and comp.startswith(pkg):
            self._activity_cache[pkg] = comp
            return comp
        return None

    def pids_of_uid(self, uid: int) -> list[tuple[int, str]]:
        out = self.adb.shell(
            f"ps -A -o PID,UID,NAME | while read p u n; do [ \"$u\" = \"{uid}\" ] && echo \"$p $n\"; done")
        res = []
        for line in out.splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2 and parts[0].isdigit():
                res.append((int(parts[0]), parts[1].strip()))
        return res

    # ---------------------------------------------------------------- sampling
    def sample(self, pkgs: list[str]) -> dict:
        """One adb round-trip: system counters + per-process memory of all apps."""
        uids = {p: self.uid_of(p) for p in pkgs}
        script = [
            "echo '##MEMINFO'; cat /proc/meminfo",
            "echo '##VMSTAT'; cat /proc/vmstat",
            "echo '##PSI'; cat /proc/pressure/memory 2>/dev/null",
            "echo '##ZRAM'; cat /sys/block/zram0/mm_stat 2>/dev/null",
            "echo '##SWAPS'; cat /proc/swaps",
            "echo '##PS'; ps -A -o PID,UID,NAME",
        ]
        uid_list = " ".join(str(u) for u in uids.values() if u is not None)
        # per-process files for every pid whose uid is one of ours
        script.append(
            "ps -A -o PID,UID,NAME | while read pid uid name; do "
            f"case \" {uid_list} \" in *\" $uid \"*) "
            "echo \"##PID $pid $uid\"; cat /proc/$pid/smaps_rollup 2>/dev/null; "
            "echo '##STAT'; cat /proc/$pid/stat 2>/dev/null; "
            "echo '##OOM'; cat /proc/$pid/oom_score_adj 2>/dev/null; "
            "echo '##FRZ'; cg=$(grep -m1 '^0::' /proc/$pid/cgroup 2>/dev/null | cut -d: -f3); "
            "[ -n \"$cg\" ] && cat /sys/fs/cgroup$cg/cgroup.freeze 2>/dev/null;;"
            " esac; done")
        out = self.adb.shell("; ".join(script), timeout=60)
        return self._parse_sample(out, pkgs, uids)

    def _parse_sample(self, out: str, pkgs: list[str], uids: dict[str, Optional[int]]) -> dict:
        sections = re.split(r"^##(MEMINFO|VMSTAT|PSI|ZRAM|SWAPS|PS|PID[^\n]*)\n", out, flags=re.M)
        sys_ = {"meminfo": {}, "vmstat": {}, "psi": {}, "zram": {}, "swaps": []}
        procs: dict[int, list[AppProc]] = {}
        ps_names: dict[int, str] = {}
        i = 1
        while i < len(sections) - 1:
            tag, body = sections[i], sections[i + 1]
            i += 2
            if tag == "MEMINFO":
                sys_["meminfo"] = P.parse_kv_kb(body)
            elif tag == "VMSTAT":
                sys_["vmstat"] = P.parse_vmstat(body)
            elif tag == "PSI":
                sys_["psi"] = P.parse_psi(body)
            elif tag == "ZRAM":
                sys_["zram"] = P.parse_zram_mm_stat(body) if body.strip() else {}
            elif tag == "SWAPS":
                sys_["swaps"] = P.parse_swaps(body)
            elif tag == "PS":
                for line in body.splitlines()[1:]:
                    parts = line.split(None, 2)
                    if len(parts) == 3 and parts[0].isdigit():
                        ps_names[int(parts[0])] = parts[2].strip()
            elif tag.startswith("PID"):
                _, pid_s, uid_s = tag.split()
                pid, uid = int(pid_s), int(uid_s)
                sm, _, rest = body.partition("##STAT\n")
                st, _, rest = rest.partition("##OOM\n")
                oom, _, frz = rest.partition("##FRZ\n")
                kv = P.parse_kv_kb(sm)
                if not kv:
                    continue  # process exited between ps and cat
                ap = AppProc(pid=pid, name=ps_names.get(pid, "?"),
                             pss_kb=kv.get("Pss", 0), pss_anon_kb=kv.get("Pss_Anon", 0),
                             pss_file_kb=kv.get("Pss_File", 0), pss_shmem_kb=kv.get("Pss_Shmem", 0),
                             swap_pss_kb=kv.get("SwapPss", 0), rss_kb=kv.get("Rss", 0),
                             majflt=P.parse_proc_stat_majflt(st) or 0)
                try:
                    ap.oom_score_adj = int(oom.strip())
                except ValueError:
                    pass
                f = frz.strip()
                ap.frozen = (f == "1") if f in ("0", "1") else None
                procs.setdefault(uid, []).append(ap)
        apps = {}
        for pkg in pkgs:
            uid = uids.get(pkg)
            plist = [p for p in procs.get(uid, []) if p.name == pkg or p.name.startswith(pkg + ":")] \
                if uid is not None else []
            apps[pkg] = AppSample(pkg=pkg, uid=uid or -1, procs=plist)
        return {"ts": time.time(), "system": sys_, "apps": apps}

    # ------------------------------------------------------------------ actions
    def launch(self, pkg: str, timeout: float = 60) -> dict:
        comp = self.launcher_activity(pkg)
        if not comp:
            return {"status": "no-activity", "launch_state": None, "total_time_ms": None}
        t0 = time.time()
        out = self.adb.shell(
            f"am start -W -a android.intent.action.MAIN -c android.intent.category.LAUNCHER "
            f"-n {shlex.quote(comp)}", timeout=timeout)
        res = P.parse_am_start(out)
        res["host_elapsed_ms"] = int((time.time() - t0) * 1000)
        res["component"] = comp
        return res

    def force_stop(self, pkg: str) -> None:
        self.adb.shell(f"am force-stop {shlex.quote(pkg)}")

    def home(self) -> None:
        self.adb.shell("input keyevent KEYCODE_HOME")

    def _freezer_paths(self, uid: int) -> list[str]:
        root = self.caps.cgroup_v2_root or "/sys/fs/cgroup"
        out = self.adb.shell(f"ls -d {root}/uid_{uid}/pid_* {root}/uid_{uid} 2>/dev/null")
        return [l.strip() for l in out.splitlines() if l.strip().startswith("/")]

    def set_frozen(self, pkg: str, frozen: bool) -> str:
        uid = self.uid_of(pkg)
        if uid is None:
            return "no-uid"
        val = "1" if frozen else "0"
        if self.caps.freezer == "cgroup.freeze":
            paths = self._freezer_paths(uid)
            if paths:
                cmds = " ; ".join(f"echo {val} > {p}/cgroup.freeze" for p in paths)
                self.adb.shell(cmds + " 2>/dev/null")
                return "cgroup.freeze"
        pids = [pid for pid, _ in self.pids_of_uid(uid)]
        if pids:
            sig = "STOP" if frozen else "CONT"
            self.adb.shell(f"kill -{sig} {' '.join(map(str, pids))} 2>/dev/null")
            return "sigstop"
        return "no-process"

    def reclaim(self, pkg: str, mode: str = "anon", bytes_hint: Optional[int] = None) -> str:
        """Push an app's pages out of DRAM (into zram / swap). Returns method used."""
        uid = self.uid_of(pkg)
        if uid is None:
            return "no-uid"
        pids = [pid for pid, _ in self.pids_of_uid(uid)]
        if not pids:
            return "no-process"
        for method in self.caps.reclaim_methods:
            if method == "memcg_v2.memory.reclaim":
                root = self.caps.cgroup_v2_root
                paths = [p for p in self._freezer_paths(uid) if p.count("/") >= 4]  # pid-level dirs
                paths = paths or [f"{root}/uid_{uid}"]
                amount = str(bytes_hint) if bytes_hint else "4G"
                ok = any("__OK__" in self.adb.shell(
                    f"echo {amount} > {p}/memory.reclaim 2>/dev/null && echo __OK__", timeout=120)
                    for p in paths)
                if ok:
                    return method
            elif method == "memcg_v1.force_empty":
                cg = self.adb.shell(f"grep -m1 memory /proc/{pids[0]}/cgroup").strip()
                path = cg.split(":")[-1] if cg else ""
                if path and path not in ("/", ""):
                    full = f"{self.caps.memcg_v1_root}{path}/memory.force_empty"
                    if "__OK__" in self.adb.shell(f"echo 0 > {full} 2>/dev/null && echo __OK__", timeout=120):
                        return method
            elif method == "proc_reclaim":
                ok = False
                for pid in pids:
                    if "__OK__" in self.adb.shell(f"echo {mode} > /proc/{pid}/reclaim 2>/dev/null && echo __OK__",
                                                  timeout=120):
                        ok = True
                if ok:
                    return method
            elif method == "balloon":
                return "balloon-needed"
        return "none"

    # ------------------------------------------------------------------ balloon
    def balloon_setup(self, max_mb: int) -> None:
        a = self.adb
        a.shell(f"mkdir -p {BALLOON_DIR}")
        if BALLOON_DIR not in a.read_file("/proc/mounts"):
            a.shell(f"mount -t tmpfs -o size={max_mb}m tmpfs {BALLOON_DIR}", check=True)
        self._balloon_blocks = len(P.parse_pid_list(
            a.shell(f"ls {BALLOON_DIR} | sed 's/blk_//'")))

    def balloon_set(self, mb: int) -> int:
        """Resize the tmpfs balloon to ~mb MiB of incompressible pages. Returns actual MiB."""
        want = max(0, mb // BALLOON_BLOCK_MB)
        cmds = []
        for i in range(self._balloon_blocks, want):
            cmds.append(f"head -c {BALLOON_BLOCK_MB * 1024 * 1024} /dev/urandom > {BALLOON_DIR}/blk_{i}")
        for i in range(want, self._balloon_blocks):
            cmds.append(f"rm -f {BALLOON_DIR}/blk_{i}")
        if cmds:
            self.adb.shell(" ; ".join(cmds), timeout=300)
        self._balloon_blocks = want
        return want * BALLOON_BLOCK_MB

    def balloon_teardown(self) -> None:
        self.adb.shell(f"umount {BALLOON_DIR} 2>/dev/null; rm -rf {BALLOON_DIR}")
        self._balloon_blocks = 0

    # ----------------------------------------------------------- swap / zram
    def setup_zram(self, size_mb: int, algo: Optional[str] = None) -> str:
        a = self.adb
        if not a.exists("/sys/block/zram0"):
            a.shell("modprobe zram 2>/dev/null")
        if not a.exists("/sys/block/zram0"):
            return "no-zram"
        if any("zram" in s["filename"] for s in P.parse_swaps(a.read_file("/proc/swaps"))):
            return "already-on"
        a.shell("echo 1 > /sys/block/zram0/reset 2>/dev/null")
        if algo:
            a.shell(f"echo {algo} > /sys/block/zram0/comp_algorithm 2>/dev/null")
        a.shell(f"echo {size_mb}M > /sys/block/zram0/disksize", check=True)
        a.shell("mkswap /dev/block/zram0", check=True)
        a.shell("swapon -p 32767 /dev/block/zram0 2>/dev/null || swapon /dev/block/zram0", check=True)
        self.caps.zram = True
        self.caps.swaps = P.parse_swaps(a.read_file("/proc/swaps"))
        return "enabled"

    def setup_swapfile(self, size_mb: int, path: str = "/data/local/tmp/cf_swapfile") -> str:
        """Optional 'flash' tier: a swap file on /data (lower priority than zram)."""
        a = self.adb
        if path in a.read_file("/proc/swaps"):
            return "already-on"
        a.shell(f"rm -f {path}; dd if=/dev/zero of={path} bs=1048576 count={size_mb} 2>/dev/null",
                timeout=600, check=True)
        a.shell(f"chmod 600 {path}; mkswap {path}", check=True)
        a.shell(f"swapon -p 100 {path} 2>/dev/null || swapon {path}", check=True)
        self.caps.swaps = P.parse_swaps(a.read_file("/proc/swaps"))
        return "enabled"

    def caps_dict(self) -> dict:
        return asdict(self.caps)
