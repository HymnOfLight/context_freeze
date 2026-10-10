"""cgroup v2 primitives for the Ubuntu throttling experiments.

Layout (created once by scripts/ubuntu/setup_cgroups.sh, owned by the desktop user):

    /sys/fs/cgroup/cf/                 cgroup.subtree_control = +cpu +cpuset +memory +io
    /sys/fs/cgroup/cf/host             the shell / runner itself (a cgroup with controllers enabled
                                       in its subtree must not hold processes, so the runner needs a leaf)
    /sys/fs/cgroup/cf/<app>            one leaf per application; its processes are placed here at launch

Moving a process between two cgroups requires write access to the destination's cgroup.procs *and*
to the cgroup.procs of their nearest common ancestor.  With the runner in cf/host and the apps in
cf/<app>, that ancestor is cf/, which the user owns - no root needed after the one-time setup.
Adopting processes that already live elsewhere (user.slice, snap scopes) needs root
(scripts/ubuntu/cf_adopt.sh).

Knobs used per tier (see ThrottleProfile):
    cpu.max            "quota period": bandwidth of one logical core = "100000 100000"
    cpuset.cpus        pin to a core set (all throttled apps share it); "" = inherit parent
    cpu.weight         share under contention (default 100)
    memory.high        soft limit: the kernel reclaims above it and throttles the allocator - no kill
    memory.max         hard limit (OOM kill above it) - only with mem_hard=true
    memory.swap.max    cap on the cgroup's swap usage
    memory.reclaim     proactive reclaim (kernel >= 5.19 / Ubuntu 22.04 HWE 6.x)

All reads/writes report errors instead of raising so a missing controller degrades to "not applied".
"""
from __future__ import annotations

import errno
import os
from dataclasses import dataclass, asdict
from typing import Optional

from .. import parsers as P

CGROUP_ROOT = os.environ.get("CF_CGROUP_ROOT", "/sys/fs/cgroup")
CF_SUBDIR = os.environ.get("CF_CGROUP_DIR", "cf")
HOST_LEAF = "host"
PERIOD_US = 100_000
TIERS = ("fg", "bg", "shrunk")


@dataclass
class ThrottleProfile:
    """What a background app is allowed to use. `bg` applies the CPU part, `shrunk` adds memory."""
    cpu_mode: str = "quota"          # quota | cpuset | both | none
    cpu_cores: float = 1.0           # quota: cpu.max = cpu_cores * period
    cpuset_cpus: str = "last"        # cpuset: "last" = highest-numbered core, or "2-3", "0,2"
    cpu_weight: Optional[int] = 20   # cpu.weight while throttled (1..10000, default 100)
    mem_frac: float = 0.5            # memory.high = mem_frac * m_fg
    mem_min_mb: int = 192            # floor for memory.high (below this the app thrashes)
    mem_hard: bool = False           # also set memory.max = mem_hard_factor * memory.high (OOM risk!)
    mem_hard_factor: float = 1.5
    swap_max_mb: Optional[int] = None    # memory.swap.max per shrunk app (None = unlimited)
    io_weight: Optional[int] = None      # io.weight while throttled (needs io controller; optional)
    proactive_reclaim: bool = True       # write memory.reclaim after lowering memory.high

    @classmethod
    def from_cfg(cls, d: Optional[dict]) -> "ThrottleProfile":
        d = dict(d or {})
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**known)

    def mem_high_bytes(self, m_fg_bytes: float) -> int:
        return int(max(self.mem_min_mb * 2 ** 20, self.mem_frac * m_fg_bytes))

    def describe(self) -> str:
        cpu = {"quota": f"{self.cpu_cores:g} core quota", "cpuset": f"cpuset {self.cpuset_cpus}",
               "both": f"{self.cpu_cores:g} core quota + cpuset {self.cpuset_cpus}", "none": "no cpu limit"}
        return (f"cpu={cpu.get(self.cpu_mode, self.cpu_mode)} weight={self.cpu_weight} "
                f"mem.high={int(self.mem_frac * 100)}% of m_fg (>= {self.mem_min_mb} MB)"
                + (f" mem.max=x{self.mem_hard_factor}" if self.mem_hard else " no mem.max")
                + (f" swap.max={self.swap_max_mb} MB" if self.swap_max_mb else " swap unlimited"))

    def to_dict(self) -> dict:
        return asdict(self)


def parse_kv(text: str) -> dict[str, int]:
    """cpu.stat / memory.stat / memory.events: `key value` lines."""
    out: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("-").isdigit():
            out[parts[0]] = int(parts[1])
    return out


def parse_cpu_list(spec: str) -> list[int]:
    """'0-2,5' -> [0, 1, 2, 5]"""
    cpus: list[int] = []
    for part in spec.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            cpus.extend(range(int(a), int(b) + 1))
        else:
            cpus.append(int(part))
    return sorted(set(cpus))


def self_cgroup_path(proc_self: str = "/proc/self/cgroup") -> str:
    """'/cf/host' from the unified hierarchy line '0::/cf/host'."""
    try:
        with open(proc_self) as f:
            for line in f:
                if line.startswith("0::"):
                    return line.strip()[3:]
    except OSError:
        pass
    return ""


def pid_cgroup_path(pid: int) -> str:
    return self_cgroup_path(f"/proc/{pid}/cgroup")


class CgroupTree:
    def __init__(self, root: str = CGROUP_ROOT, subdir: str = CF_SUBDIR):
        self.root = root
        self.subdir = subdir
        self.base = os.path.join(root, subdir)
        self.errors: list[str] = []

    # ------------------------------------------------------------- basics
    def path(self, name: str = "") -> str:
        return os.path.join(self.base, name) if name else self.base

    def exists(self, name: str = "") -> bool:
        return os.path.isdir(self.path(name))

    def available(self) -> bool:
        return os.path.isfile(os.path.join(self.base, "cgroup.controllers"))

    def controllers(self) -> list[str]:
        return self.read("", "cgroup.controllers").split()

    def subtree_control(self) -> list[str]:
        return self.read("", "cgroup.subtree_control").split()

    def writable(self) -> bool:
        return os.access(os.path.join(self.base, "cgroup.procs"), os.W_OK) and os.access(self.base, os.W_OK)

    def inside(self, pid: Optional[int] = None) -> bool:
        """Is the given process (default: us) somewhere below cf/?"""
        p = pid_cgroup_path(pid) if pid else self_cgroup_path()
        return p == "/" + self.subdir or p.startswith("/" + self.subdir + "/")

    def leaf_of(self, pid: int) -> Optional[str]:
        """'wechat' for a process in /cf/wechat, None when outside the tree."""
        p = pid_cgroup_path(pid)
        pre = "/" + self.subdir + "/"
        return p[len(pre):].split("/")[0] if p.startswith(pre) else None

    def read(self, name: str, file: str) -> str:
        try:
            with open(os.path.join(self.path(name), file)) as f:
                return f.read().strip()
        except OSError:
            return ""

    def write(self, name: str, file: str, value: str) -> Optional[str]:
        """Returns None on success, else an error string (also appended to self.errors)."""
        p = os.path.join(self.path(name), file)
        try:
            with open(p, "w") as f:
                f.write(str(value))
            return None
        except OSError as e:
            msg = f"{p} <- {value!r}: {errno.errorcode.get(e.errno, e.errno)} {e.strerror}"
            self.errors.append(msg)
            return msg

    def ensure(self, name: str) -> str:
        p = self.path(name)
        if not os.path.isdir(p):
            os.makedirs(p, exist_ok=True)
        return p

    def remove(self, name: str) -> bool:
        try:
            os.rmdir(self.path(name))
            return True
        except OSError:
            return False

    def leaves(self) -> list[str]:
        if not os.path.isdir(self.base):
            return []
        return sorted(d for d in os.listdir(self.base)
                      if os.path.isdir(os.path.join(self.base, d)) and d != HOST_LEAF)

    # -------------------------------------------------------------- procs
    def procs(self, name: str) -> list[int]:
        return [int(x) for x in self.read(name, "cgroup.procs").split() if x.isdigit()]

    def populated(self, name: str) -> bool:
        ev = parse_kv(self.read(name, "cgroup.events"))
        return bool(ev.get("populated", 1 if self.procs(name) else 0))

    def attach(self, name: str, pids: list[int]) -> tuple[list[int], dict[int, str]]:
        ok, failed = [], {}
        for pid in pids:
            err = self.write(name, "cgroup.procs", str(pid))
            if err is None:
                ok.append(pid)
            else:
                failed[pid] = err
        return ok, failed

    def attach_self(self, name: str) -> Optional[str]:
        return self.write(name, "cgroup.procs", str(os.getpid()))

    # --------------------------------------------------------------- cpus
    def effective_cpus(self) -> list[int]:
        spec = self.read("", "cpuset.cpus.effective") or self.read("", "cpuset.cpus")
        if not spec:
            try:
                with open(os.path.join(self.root, "cpuset.cpus.effective")) as f:
                    spec = f.read().strip()
            except OSError:
                spec = f"0-{max(0, (os.cpu_count() or 1) - 1)}"
        return parse_cpu_list(spec)

    def resolve_cpuset(self, spec: str) -> str:
        if spec == "last":
            return str(self.effective_cpus()[-1])
        if spec == "first":
            return str(self.effective_cpus()[0])
        return spec

    # ------------------------------------------------------------- limits
    def set_limits(self, name: str, *, cpu_max: Optional[str] = None, cpuset: Optional[str] = None,
                   cpu_weight: Optional[int] = None, memory_high: Optional[str] = None,
                   memory_max: Optional[str] = None, swap_max: Optional[str] = None,
                   io_weight: Optional[int] = None) -> dict:
        """Write the given knobs; values None are left untouched. Returns {knob: value | 'ERR ...'}."""
        applied: dict[str, str] = {}
        ctrl = set(self.controllers())
        todo = [("cpu.max", cpu_max, "cpu"), ("cpuset.cpus", cpuset, "cpuset"), ("cpu.weight", cpu_weight, "cpu"),
                ("memory.high", memory_high, "memory"), ("memory.max", memory_max, "memory"),
                ("memory.swap.max", swap_max, "memory"), ("io.weight", io_weight, "io")]
        for file, value, need in todo:
            if value is None:
                continue
            if ctrl and need not in ctrl:
                applied[file] = f"ERR controller {need} not enabled"
                continue
            err = self.write(name, file, str(value))
            applied[file] = str(value) if err is None else "ERR " + err.split(": ", 1)[-1]
        return applied

    def clear_limits(self, name: str) -> dict:
        return self.set_limits(name, cpu_max=f"max {PERIOD_US}", cpuset="", cpu_weight=100,
                               memory_high="max", memory_max="max", swap_max="max",
                               io_weight=100 if "io" in self.controllers() else None)

    def reclaim(self, name: str, nbytes: int) -> str:
        """Proactive reclaim of up to nbytes (memory.reclaim). 'unsupported' on kernels < 5.19;
        'partial' when the kernel could not reclaim the full amount (EAGAIN) - not an error."""
        if nbytes <= 0:
            return "nothing"
        if not os.path.exists(os.path.join(self.path(name), "memory.reclaim")):
            return "unsupported"
        err = self.write(name, "memory.reclaim", str(int(nbytes)))
        if err is None:
            return "memory.reclaim"
        if "EAGAIN" in err:
            self.errors.pop()
            return "partial"
        return "error"

    # -------------------------------------------------------------- stats
    def stats(self, name: str) -> dict:
        def _int(s: str) -> Optional[int]:
            try:
                return int(s)
            except ValueError:
                return None
        mem_stat = parse_kv(self.read(name, "memory.stat"))
        out = {
            "memory_current": _int(self.read(name, "memory.current")),
            "swap_current": _int(self.read(name, "memory.swap.current")),
            "memory_high": self.read(name, "memory.high") or None,
            "memory_max": self.read(name, "memory.max") or None,
            "swap_max": self.read(name, "memory.swap.max") or None,
            "cpu_max": self.read(name, "cpu.max") or None,
            "cpuset": self.read(name, "cpuset.cpus.effective") or None,
            "cpu_weight": _int(self.read(name, "cpu.weight")),
            "events": parse_kv(self.read(name, "memory.events")),
            "cpu": parse_kv(self.read(name, "cpu.stat")),
            "pgmajfault": mem_stat.get("pgmajfault"),
            "pgfault": mem_stat.get("pgfault"),
            "refault_anon": mem_stat.get("workingset_refault_anon"),
            "refault_file": mem_stat.get("workingset_refault_file"),
            "anon": mem_stat.get("anon"), "file": mem_stat.get("file"),
            "psi_mem": P.parse_psi(self.read(name, "memory.pressure")),
            "psi_cpu": P.parse_psi(self.read(name, "cpu.pressure")),
            "psi_io": P.parse_psi(self.read(name, "io.pressure")),
            "n_procs": len(self.procs(name)),
        }
        return out


def tier_limits(tree: CgroupTree, profile: ThrottleProfile, tier: str,
                m_fg_bytes: Optional[float] = None) -> dict:
    """The knob values for a tier (None = clear). Pure function of the profile, unit-testable."""
    if tier not in TIERS:
        raise ValueError(f"tier must be one of {TIERS}, got {tier!r}")
    if tier == "fg":
        return {"cpu_max": f"max {PERIOD_US}", "cpuset": "", "cpu_weight": 100,
                "memory_high": "max", "memory_max": "max", "swap_max": "max",
                "io_weight": 100 if profile.io_weight else None}
    lim: dict = {"memory_high": "max", "memory_max": "max", "swap_max": "max"}
    if profile.cpu_mode in ("quota", "both"):
        lim["cpu_max"] = f"{int(PERIOD_US * profile.cpu_cores)} {PERIOD_US}"
    else:
        lim["cpu_max"] = f"max {PERIOD_US}"
    if profile.cpu_mode in ("cpuset", "both"):
        lim["cpuset"] = tree.resolve_cpuset(profile.cpuset_cpus)
    else:
        lim["cpuset"] = ""
    lim["cpu_weight"] = profile.cpu_weight if profile.cpu_weight else 100
    lim["io_weight"] = profile.io_weight
    if tier == "shrunk":
        high = profile.mem_high_bytes(m_fg_bytes or 0)
        lim["memory_high"] = str(high)
        if profile.mem_hard:
            lim["memory_max"] = str(int(high * profile.mem_hard_factor))
        if profile.swap_max_mb is not None:
            lim["swap_max"] = str(int(profile.swap_max_mb * 2 ** 20))
    return lim


def apply_tier(tree: CgroupTree, name: str, tier: str, profile: ThrottleProfile,
               m_fg_bytes: Optional[float] = None) -> dict:
    """Apply a tier to cf/<name>. For `shrunk` also reclaims down to memory.high when supported.
    Returns {"tier", "limits": {...}, "reclaim": how, "reclaim_bytes": n}."""
    lim = tier_limits(tree, profile, tier, m_fg_bytes)
    # raising limits must happen before lowering others would fail (e.g. memory.max < memory.high is
    # legal but pointless); order: cpu first, then memory
    applied = tree.set_limits(name, **lim)
    res = {"tier": tier, "limits": applied, "reclaim": None, "reclaim_bytes": 0}
    if tier == "shrunk" and profile.proactive_reclaim:
        cur = tree.stats(name).get("memory_current") or 0
        high = int(lim["memory_high"])
        if cur > high:
            res["reclaim"] = tree.reclaim(name, cur - high)
            res["reclaim_bytes"] = cur - high
        else:
            res["reclaim"] = "nothing"
    return res
