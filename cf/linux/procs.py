"""/proc sampling for the Linux back-end (no root needed for the user's own processes)."""
from __future__ import annotations

import os
import platform
import re
import time
from dataclasses import dataclass
from typing import Iterable, Optional

from .. import parsers as P

PAGE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


def read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


@dataclass
class ProcInfo:
    pid: int
    name: str = ""
    cmdline: str = ""
    pss_kb: int = 0
    pss_anon_kb: int = 0
    pss_file_kb: int = 0
    pss_shmem_kb: int = 0
    swap_pss_kb: int = 0
    rss_kb: int = 0
    majflt: int = 0
    cpu_ms: int = 0              # utime + stime
    read_bytes: int = 0          # /proc/<pid>/io (storage reads)
    write_bytes: int = 0
    rchar: int = 0
    wchar: int = 0
    state: str = ""
    threads: int = 0
    age_s: float = 0.0           # seconds since the process started (short-lived helpers are ignored by state_check)


def cmdline(pid: int) -> str:
    return read(f"/proc/{pid}/cmdline").replace("\0", " ").strip()


def uptime_s() -> float:
    try:
        return float(read("/proc/uptime").split()[0])
    except (ValueError, IndexError):
        return 0.0


def comm(pid: int) -> str:
    return read(f"/proc/{pid}/comm").strip()


def exe(pid: int) -> str:
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return ""


def parse_stat(text: str) -> dict:
    """/proc/<pid>/stat -> {state, ppid, majflt, utime, stime, threads, sid}. comm may contain spaces."""
    try:
        head, _, rest = text.rpartition(")")
        f = rest.split()
        return {"state": f[0], "ppid": int(f[1]), "sid": int(f[3]), "majflt": int(f[9]),
                "utime": int(f[11]), "stime": int(f[12]), "threads": int(f[17]),
                "starttime": int(f[19]) if len(f) > 19 else 0}
    except (ValueError, IndexError):
        return {}


def parse_io(text: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for line in text.splitlines():
        k, _, v = line.partition(":")
        v = v.strip()
        if v.isdigit():
            out[k.strip()] = int(v)
    return out


def proc_info(pid: int, with_smaps: bool = True) -> Optional[ProcInfo]:
    st = parse_stat(read(f"/proc/{pid}/stat"))
    if not st:
        return None
    pi = ProcInfo(pid=pid, name=comm(pid), cmdline=cmdline(pid)[:200], majflt=st["majflt"],
                  cpu_ms=int((st["utime"] + st["stime"]) * 1000 / CLK_TCK), state=st["state"],
                  threads=st["threads"], age_s=round(max(0.0, uptime_s() - st.get("starttime", 0) / CLK_TCK), 1))
    if with_smaps:
        kv = P.parse_kv_kb(read(f"/proc/{pid}/smaps_rollup"))
        pi.pss_kb = kv.get("Pss", 0)
        pi.pss_anon_kb = kv.get("Pss_Anon", 0)
        pi.pss_file_kb = kv.get("Pss_File", 0)
        pi.pss_shmem_kb = kv.get("Pss_Shmem", 0)
        pi.swap_pss_kb = kv.get("SwapPss", 0)
        pi.rss_kb = kv.get("Rss", 0)
    io = parse_io(read(f"/proc/{pid}/io"))
    pi.read_bytes, pi.write_bytes = io.get("read_bytes", 0), io.get("write_bytes", 0)
    pi.rchar, pi.wchar = io.get("rchar", 0), io.get("wchar", 0)
    return pi


def all_pids() -> list[int]:
    return [int(d) for d in os.listdir("/proc") if d.isdigit()]


def find_pids(pattern: str, pids: Optional[Iterable[int]] = None, uid: Optional[int] = None) -> list[int]:
    """Processes whose cmdline / exe / comm matches the regex (own uid by default)."""
    rx = re.compile(pattern)
    me = os.getuid() if uid is None else uid
    hits = []
    for pid in (pids if pids is not None else all_pids()):
        try:
            if os.stat(f"/proc/{pid}").st_uid != me:
                continue
        except OSError:
            continue
        if rx.search(cmdline(pid)) or rx.search(exe(pid)) or rx.search(comm(pid)):
            hits.append(pid)
    return hits


def children_of(root_pids: Iterable[int]) -> list[int]:
    """root pids plus all their descendants (walks /proc once)."""
    ppid: dict[int, int] = {}
    for pid in all_pids():
        st = parse_stat(read(f"/proc/{pid}/stat"))
        if st:
            ppid[pid] = st["ppid"]
    want = set(root_pids)
    changed = True
    while changed:
        changed = False
        for pid, pp in ppid.items():
            if pp in want and pid not in want:
                want.add(pid)
                changed = True
    return sorted(want)


def session_pids(sid: int) -> list[int]:
    out = []
    for pid in all_pids():
        st = parse_stat(read(f"/proc/{pid}/stat"))
        if st and st.get("sid") == sid:
            out.append(pid)
    return out


# ------------------------------------------------------------------ system
def zram_mm_stat(dev: str = "zram0") -> dict:
    txt = read(f"/sys/block/{dev}/mm_stat")
    return P.parse_zram_mm_stat(txt) if txt.strip() else {}


def system_sample() -> dict:
    la = read("/proc/loadavg").split()
    return {
        "meminfo": P.parse_kv_kb(read("/proc/meminfo")),
        "vmstat": P.parse_vmstat(read("/proc/vmstat")),
        "psi": P.parse_psi(read("/proc/pressure/memory")),
        "psi_cpu": P.parse_psi(read("/proc/pressure/cpu")),
        "psi_io": P.parse_psi(read("/proc/pressure/io")),
        "zram": zram_mm_stat(),
        "swaps": P.parse_swaps(read("/proc/swaps")),
        "loadavg": [float(x) for x in la[:3]] if len(la) >= 3 else [],
        "ts": time.time(),
    }


def os_release() -> dict[str, str]:
    out = {}
    for line in read("/etc/os-release").splitlines():
        k, _, v = line.partition("=")
        if k:
            out[k] = v.strip().strip('"')
    return out


def host_info() -> dict:
    mi = P.parse_kv_kb(read("/proc/meminfo"))
    osr = os_release()
    return {"kernel": platform.release(), "arch": platform.machine(),
            "distro": osr.get("PRETTY_NAME", ""), "cpu_count": os.cpu_count() or 0,
            "mem_total_kb": mi.get("MemTotal", 0), "swap_total_kb": mi.get("SwapTotal", 0),
            "page_size": PAGE, "hypervisor": _hypervisor()}


def _hypervisor() -> str:
    txt = read("/sys/class/dmi/id/sys_vendor").strip() + " " + read("/sys/class/dmi/id/product_name").strip()
    txt = txt.strip()
    if not txt:
        # arm64 VMs have no DMI; the device tree compatible string names the machine
        txt = read("/proc/device-tree/compatible").replace("\0", " ").strip()
    return txt[:80]
