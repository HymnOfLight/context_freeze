"""LinuxDesktop: the Ubuntu counterpart of cf.device.Device.

    launch(app)           start the app into cf/<app> (or just switch to its window when it runs),
                          bring it to the foreground, measure activate / ping latency
    set_tier(app, tier)   fg | bg | shrunk  -> cgroup knobs (cpu.max, cpuset, memory.high, swap)
    sample(apps)          per-app PSS / swap / cpu / io / cgroup stats + system counters
    state_check(...)      did any process, window or state disappear (OOM kill, crash)?

Apps of kind "mobile" (the app's own Android build in Waydroid, cf.linux.mobile) go through the
same interface: their processes are host processes (adopted into cf/<app> like strays), their
window is the shared Android display, and launch() = activate that window + `am start -W`.

Nothing here freezes or kills processes.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

from .. import parsers as P
from ..logging_util import as_logger
from . import apps as A
from . import blankdoc
from . import procs as PR
from . import x11
from .cgroup import CgroupTree, ThrottleProfile, HOST_LEAF, apply_tier, squeeze
from .mobile import MobileRuntime
from .notify import NotifyCounter

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STABLE_PID_AGE_S = 5.0


@dataclass
class AppSampleL:
    app: str
    procs: list[PR.ProcInfo]
    cgroup: dict = field(default_factory=dict)
    window: Optional[int] = None
    tier: str = "fg"
    stray_pids: list[int] = field(default_factory=list)
    # the processes that are the app's state; None = all of them.  An app that keeps starting a tool
    # (7z, z3) for every job has processes that come and go by design
    state_pids: Optional[list[int]] = None

    @property
    def alive(self) -> bool:
        return bool(self.procs)

    def total(self, attr: str) -> int:
        return sum(getattr(p, attr) or 0 for p in self.procs)

    def to_dict(self) -> dict:
        cg = self.cgroup or {}
        cpu = cg.get("cpu") or {}
        return {"pkg": self.app, "alive": self.alive, "pids": [p.pid for p in self.procs],
                # helpers such as notify-send / xdg-open live for a fraction of a second; only
                # processes older than this count as "state" whose disappearance matters
                "stable_pids": [p.pid for p in self.procs if p.age_s >= STABLE_PID_AGE_S
                                and (self.state_pids is None or p.pid in self.state_pids)],
                "pss_kb": self.total("pss_kb"), "pss_anon_kb": self.total("pss_anon_kb"),
                "pss_file_kb": self.total("pss_file_kb"), "pss_shmem_kb": self.total("pss_shmem_kb"),
                "swap_pss_kb": self.total("swap_pss_kb"), "rss_kb": self.total("rss_kb"),
                "majflt": self.total("majflt"), "cpu_ms": self.total("cpu_ms"),
                "read_bytes": self.total("read_bytes"), "write_bytes": self.total("write_bytes"),
                "threads": self.total("threads"), "window": self.window, "tier": self.tier,
                "stray_pids": self.stray_pids,
                "cg_mem_mb": round((cg.get("memory_current") or 0) / 2 ** 20, 1),
                "cg_swap_mb": round((cg.get("swap_current") or 0) / 2 ** 20, 1),
                "cg_usage_ms": (cpu.get("usage_usec") or 0) // 1000,
                "cg_throttled_ms": (cpu.get("throttled_usec") or 0) // 1000,
                "cg_nr_throttled": cpu.get("nr_throttled") or 0,
                "cg_events": cg.get("events") or {},
                "cg_pgmajfault": cg.get("pgmajfault"),
                "cg_pgsteal": cg.get("pgsteal"), "cg_pgscan": cg.get("pgscan"),
                "cg_pswpout": cg.get("pswpout") if cg.get("pswpout") is not None else cg.get("zswpout"),
                "cg_limits": {k: cg.get(k) for k in ("cpu_max", "cpuset", "cpu_weight", "memory_high",
                                                     "memory_max", "swap_max")},
                "cg_psi_mem_full_us": (cg.get("psi_mem") or {}).get("full_total"),
                "cg_psi_cpu_some_us": (cg.get("psi_cpu") or {}).get("some_total")}

    def procs_dict(self) -> list[dict]:
        """Per-process memory (kB) for the per-process accounting (cfg per_process)."""
        return [{"pid": p.pid, "name": p.name, "pss_kb": p.pss_kb, "pss_anon_kb": p.pss_anon_kb,
                 "pss_file_kb": p.pss_file_kb, "swap_pss_kb": p.swap_pss_kb, "rss_kb": p.rss_kb,
                 "cpu_ms": p.cpu_ms, "majflt": p.majflt} for p in self.procs]


PING_MISSES_TO_GIVE_UP = 2


class LinuxDesktop:
    def __init__(self, cat: Optional[dict[str, A.LinuxApp]] = None, tree: Optional[CgroupTree] = None,
                 profile: Optional[ThrottleProfile] = None, log=None, notifications: bool = True,
                 sudo_adopt: bool = False, log_dir: str = "results/app_logs", launch_timeout: float = 90.0,
                 ping_timeout: float = 15.0, mobile: Optional[MobileRuntime] = None):
        self.cat = cat or A.CATALOG
        self.tree = tree or CgroupTree()
        self.profile = profile or ThrottleProfile()
        self.log = as_logger(log)
        self.mobile = mobile
        self.sudo_adopt = sudo_adopt
        self.log_dir = log_dir
        self.launch_timeout = launch_timeout
        self.ping_timeout = ping_timeout
        self.tiers: dict[str, str] = {}
        self.caps: dict = {}
        self.pinger: Optional[x11.Pinger] = None
        self.notify = NotifyCounter() if notifications else None
        self.spawned: dict[str, subprocess.Popen] = {}
        self._warned_unmanaged: set[str] = set()
        self._ping_misses: dict[str, int] = {}       # consecutive unanswered pings per app
        self.ping_dead: set[str] = set()             # apps that advertise _NET_WM_PING but never answer
        self._bench_off: dict[str, int] = {}         # bytes of each bench app's job log already read
        self.net = None                              # cf.linux.netstat.SockSampler (net_start)
        self.net_apps: list[str] = []
        self._probes: dict[str, subprocess.Popen] = {}   # app -> cf.linux.net_probe process in <app>/cfprobe

    # ------------------------------------------------------------- probing
    def probe(self) -> dict:
        t = self.tree
        c = PR.host_info()
        c.update({
            "cgroup_base": t.base, "cgroup_available": t.available(), "cgroup_writable": t.writable(),
            "runner_inside_cf": t.inside(), "runner_cgroup": PR.read("/proc/self/cgroup").strip(),
            "controllers": t.controllers(), "subtree_control": t.subtree_control(),
            "memory_reclaim": os.path.exists(os.path.join(t.base, "memory.reclaim")),
            "host_leaf": t.exists(HOST_LEAF),
            "effective_cpus": t.effective_cpus() if t.available() else [],
            "display": os.environ.get("DISPLAY", ""), "session_type": x11.session_type(),
            "wm": x11.wm_name() if x11.have_display() else "", "x11_tools_missing": x11.tools_missing(),
            "swaps": P.parse_swaps(PR.read("/proc/swaps")), "zram": os.path.exists("/sys/block/zram0"),
            "profile": self.profile.to_dict(),
        })
        if self.pinger is None and x11.have_display():
            self.pinger = x11.Pinger()
        c["ping_available"] = bool(self.pinger and self.pinger.available)
        c["ping_error"] = self.pinger.err if self.pinger else "no DISPLAY"
        if self.notify and not self.notify.running:
            c["notify_monitor"] = self.notify.start()
            c["notify_error"] = self.notify.error
        c["mobile"] = self.mobile.probe() if self.mobile is not None else {"available": False, "backend": None}
        c["apps"] = {id_: {"installed": self.is_installed(a), "cmd": a.cmd, "category": a.category,
                           "linux": a.linux, "substitute": a.substitute, "kind": a.kind, "package": a.package}
                     for id_, a in self.cat.items()}
        self.caps = c
        return c

    def caps_dict(self) -> dict:
        return dict(self.caps)

    def is_installed(self, a: A.LinuxApp) -> bool:
        return A.is_installed(a, mobile=self.mobile)

    def available(self, id_: str) -> bool:
        return id_ in self.cat and self.is_installed(self.cat[id_])

    def is_mobile(self, id_: str) -> bool:
        return id_ in self.cat and self.cat[id_].is_mobile

    # ----------------------------------------------------------- processes
    def pids(self, id_: str) -> list[int]:
        return self.tree.procs(id_) if self.tree.exists(id_) else []

    def stray_pids(self, id_: str, known: Optional[list[int]] = None) -> list[int]:
        """Processes that look like the app (regex) but are not in its cgroup (snap scope, D-Bus
        activation, started by hand before setup).  Mobile apps: the Android app's host processes
        (zygote forks them into the container's cgroup; every one of them starts as a stray)."""
        a = self.cat[id_]
        inside = set(known if known is not None else self.pids(id_))
        if a.is_mobile:
            hits = self.mobile.host_pids(a.package) if self.mobile is not None else []
        else:
            hits = PR.find_pids(a.match)
        return [p for p in hits if p not in inside and p != os.getpid()]

    def mem_peak_kb(self, id_: str) -> Optional[int]:
        """The most memory the app's cgroup has held since it was created (memory.peak, kernel >= 5.19;
        anonymous memory and page cache - what memory.high counts); None when the kernel has no such file."""
        v = self.tree.read(id_, "memory.peak")
        return int(v) // 1024 if v.isdigit() else None

    def adopt(self, id_: str) -> dict:
        """Move stray processes (and their children) into cf/<app>."""
        stray = PR.children_of(self.stray_pids(id_))
        if not stray:
            return {"moved": [], "failed": {}}
        self.tree.ensure(id_)
        ok, failed = self.tree.attach(id_, stray)
        if failed and self.sudo_adopt:
            helper = os.path.join(REPO_ROOT, "scripts", "ubuntu", "cf_adopt.sh")
            cmd = ["sudo", "-n", helper, self.tree.path(id_), *map(str, failed)]
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if p.returncode == 0:
                ok += list(failed)
                failed = {}
            else:
                failed = {pid: (p.stderr or p.stdout).strip()[:120] for pid in failed}
        return {"moved": ok, "failed": failed}

    # -------------------------------------------------------------- windows
    def window(self, id_: str, infos: Optional[list[dict]] = None, pids: Optional[list[int]] = None) -> Optional[dict]:
        a = self.cat[id_]
        if a.is_mobile:
            # the shared Android display (nested Weston); its _NET_WM_PID is weston's, not the app's
            crx = a.wm_class
            if crx == A.MOBILE_WM_CLASS and self.mobile is not None and self.mobile.wm_class:
                crx = self.mobile.wm_class
            hits = x11.windows_for([], crx, a.name_rx or None, infos)
            return hits[0] if hits else None
        pids = self.pids(id_) if pids is None else pids
        hits = x11.windows_for(pids, a.wm_class, a.name_rx or None, infos)
        return hits[0] if hits else None

    def _spawn(self, id_: str) -> Optional[subprocess.Popen]:
        a = self.cat[id_]
        if not a.cmd:
            return None
        self.tree.ensure(id_)
        os.makedirs(self.log_dir, exist_ok=True)
        logf = open(os.path.join(self.log_dir, f"{id_}.log"), "ab")
        procs_file = os.path.join(self.tree.path(id_), "cgroup.procs")

        def _enter_cgroup() -> None:
            try:
                with open(procs_file, "w") as f:
                    f.write(str(os.getpid()))
            except OSError:
                pass            # parent re-tries below and reports

        env = dict(os.environ)
        env.setdefault("PYTHONPATH", REPO_ROOT)
        if blankdoc.BLANK_TOKEN in a.cmd:
            blankdoc.ensure()
        cmd = A.launch_cmd(a)
        if cmd != a.cmd:
            logf.write(f"== cf: '{a.cmd.split()[0]}' is not on PATH, starting {cmd}\n".encode())
            logf.flush()
        p = subprocess.Popen(cmd, shell=True, cwd=REPO_ROOT, env=env, stdin=subprocess.DEVNULL,
                             stdout=logf, stderr=subprocess.STDOUT, start_new_session=True,
                             preexec_fn=_enter_cgroup)
        logf.close()
        if p.pid not in self.tree.procs(id_):
            ok, failed = self.tree.attach(id_, [p.pid])
            if failed and id_ not in self._warned_unmanaged:
                self._warned_unmanaged.add(id_)
                self.log.warn(f"{id_}: cannot place pid {p.pid} into {self.tree.path(id_)} "
                              f"({list(failed.values())[0]}); limits will NOT apply. Run the experiment from "
                              f"a shell entered with scripts/ubuntu/setup_cgroups.sh")
        self.spawned[id_] = p
        return p

    def _launch_mobile(self, id_: str, timeout: float) -> dict:
        """Mobile app: activate the Android display's X11 window, then `am start -W` the app inside
        Android.  total_time_ms = activate_ms + Android's TotalTime; LaunchState comes from Android
        (HOT / WARM / COLD as the Android back-end of this repo reports it)."""
        a = self.cat[id_]
        mb = self.mobile
        t0 = time.monotonic()
        res: dict = {"status": "ok", "launch_state": None, "total_time_ms": None, "activate_ms": None,
                     "ping_ms": None, "launch_ms": None, "wid": None, "pid": None, "component": a.package,
                     "mobile": True}
        if mb is None or not (mb.available or mb.ensure_session()):
            res.update(status="no-android-runtime", launch_state="TIMEOUT",
                       total_time_ms=int((time.monotonic() - t0) * 1000))
            return res
        running = bool(self.pids(id_) or mb.host_pids(a.package))
        win = self.window(id_)
        deadline = time.monotonic() + min(timeout, 30)
        while win is None and time.monotonic() < deadline:   # the display window appears with the session
            time.sleep(0.5)
            win = self.window(id_)
        if win is None:
            res.update(status="no-window", launch_state="TIMEOUT", total_time_ms=int((time.monotonic() - t0) * 1000))
            return res
        res["wid"] = win["wid"]
        was_front = x11.active_window() == win["wid"] and mb.foreground_pkg() == a.package
        act = x11.activate(win["wid"], timeout=min(timeout, 20))
        res["activate_ms"] = act.get("activate_ms")
        if not act.get("ok"):
            res["status"] = act.get("error", "activate-failed")
        am = mb.launch(a.package, timeout=timeout)
        res["am"] = {k: am.get(k) for k in ("status", "launch_state", "this_time_ms", "total_time_ms",
                                            "wait_time_ms", "component", "am_elapsed_ms")}
        state = am.get("launch_state")
        if am.get("status") not in (None, "ok") and res["status"] == "ok":
            res["status"] = f"am-{am.get('status')}"
        if state is None:
            state = "HOT" if running else "COLD"
        if was_front or state == "FRONT":
            res["launch_state"] = "FRONT"
        else:
            res["launch_state"] = state
            res["launch_ms"] = am.get("total_time_ms")
            res["total_time_ms"] = int((res["activate_ms"] or 0) + (am.get("total_time_ms") or 0))
        if state == "TIMEOUT":
            res["status"] = res["status"] if res["status"] != "ok" else "am-timeout"
        time.sleep(0.5)      # let the new process tree settle before adopting it
        r = self.adopt(id_)
        res["adopted"] = r["moved"]
        if r["failed"] and id_ not in self._warned_unmanaged:
            self._warned_unmanaged.add(id_)
            self.log.warn(f"{id_}: Android processes {sorted(r['failed'])} could not be moved into {self.tree.path(id_)} "
                          f"({list(r['failed'].values())[0]}); limits will NOT apply. Enable sudo_adopt "
                          f"(scripts/ubuntu/setup_cgroups.sh installs the sudoers rule for cf_adopt.sh)")
        pids = self.pids(id_)
        res["pid"] = pids[0] if pids else None
        res["host_elapsed_ms"] = int((time.monotonic() - t0) * 1000)
        self.tiers[id_] = "fg"
        return res

    def launch(self, id_: str, timeout: Optional[float] = None) -> dict:
        """Bring the app to the foreground (starting it when needed) and measure the switch."""
        timeout = timeout or self.launch_timeout
        a = self.cat[id_]
        if a.is_mobile:
            return self._launch_mobile(id_, timeout)
        t0 = time.monotonic()
        pids = self.pids(id_)
        win = self.window(id_, pids=pids)
        res: dict = {"status": "ok", "launch_state": None, "total_time_ms": None, "activate_ms": None,
                     "ping_ms": None, "launch_ms": None, "wid": None, "pid": None, "component": a.cmd}
        if win is None:
            res["launch_state"] = "COLD" if not pids else "REOPEN"
            if self._spawn(id_) is None:
                res.update(status="no-command", launch_state=None)
                return res
            deadline = time.monotonic() + timeout
            proc = self.spawned.get(id_)
            while time.monotonic() < deadline and win is None:
                time.sleep(0.25)
                win = self.window(id_)
                if win is None and proc is not None and proc.poll() is not None and not self.pids(id_):
                    # the launcher exited and left nothing behind: crashed (see results/app_logs/<id>.log)
                    res.update(status=f"exited-{proc.returncode}", launch_state="TIMEOUT",
                               total_time_ms=int((time.monotonic() - t0) * 1000))
                    return res
            if win is None:
                res.update(status="no-window", launch_state="TIMEOUT",
                           total_time_ms=int((time.monotonic() - t0) * 1000))
                return res
            res["launch_ms"] = int((time.monotonic() - t0) * 1000)
            time.sleep(0.3)      # let the WM map / place the new window before activating it
        else:
            res["launch_state"] = "HOT"
        res["wid"], res["pid"] = win["wid"], win.get("pid")
        if x11.active_window() == win["wid"] and res["launch_state"] == "HOT":
            res["launch_state"] = "FRONT"            # already in the foreground: no latency sample
        act = x11.activate(win["wid"], timeout=min(timeout, 20))
        res["activate_ms"] = act.get("activate_ms")
        if not act.get("ok"):
            res["status"] = act.get("error", "activate-failed")
        if self.pinger and self.pinger.available and id_ in self.ping_dead:
            res["ping_ms"], res["ping_supported"] = None, False      # learned: it never answers, do not wait
        elif self.pinger and self.pinger.available:
            res["ping_ms"] = self.pinger.ping(win["wid"], timeout=self.ping_timeout)
            res["ping_supported"] = self.pinger.supports(win["wid"])
            if res["ping_supported"] and res["ping_ms"] is None:
                res["status"] = "ping-timeout"
                # 微信 / 钉钉 / 中望 list _NET_WM_PING in WM_PROTOCOLS but never reply (Qt frameless
                # windows); after two misses while the activation itself worked, treat the app as
                # ping-unsupported instead of paying ping_timeout on every visit
                self._ping_misses[id_] = self._ping_misses.get(id_, 0) + 1
                if act.get("ok") and self._ping_misses[id_] >= PING_MISSES_TO_GIVE_UP:
                    self.ping_dead.add(id_)
                    self.log.warn(f"{id_}: never answers _NET_WM_PING ({self._ping_misses[id_]} misses with a "
                                  f"successful activate) - ping disabled for it (ping_ms stays None)")
            else:
                self._ping_misses[id_] = 0
        if res["launch_state"] != "FRONT":
            res["total_time_ms"] = int((res["launch_ms"] or 0) + (res["activate_ms"] or 0) + (res["ping_ms"] or 0))
        res["host_elapsed_ms"] = int((time.monotonic() - t0) * 1000)
        self.tiers[id_] = "fg"
        return res

    def smooth_probe(self, id_: str, wid: Optional[int], n: int = 5, every_s: float = 0.2,
                     timeout_s: float = 1.0) -> dict:
        """How smoothly the app reacts right after it was brought to the front: `n` _NET_WM_PING round
        trips, one every `every_s` seconds.  A ping the event loop answers late (or not within
        `timeout_s` -> None) is a stall the user would feel.  {"supported": False} for apps that
        do not answer pings at all (Qt frameless windows, the Android display)."""
        out: dict = {"supported": False, "pings_ms": []}
        if not wid or n <= 0 or self.is_mobile(id_) or id_ in self.ping_dead:
            return out
        if not (self.pinger and self.pinger.available and self.pinger.supports(wid)):
            return out
        out["supported"] = True
        for i in range(n):
            t0 = time.monotonic()
            out["pings_ms"].append(self.pinger.ping(wid, timeout=timeout_s))
            rest = every_s - (time.monotonic() - t0)
            if rest > 0 and i < n - 1:
                time.sleep(rest)
        return out

    # ------------------------------------------------------------ bench apps
    def net_start(self, ids: list[str], every_s: float = 1.0, loopback: bool = False, by: str = "auto",
                  strays: bool = False, probe_host: str = "", probe_every_s: float = 0.5) -> None:
        """Watch the TCP sockets of these apps (cf.linux.netstat): received bytes and round trip
        time per sample interval, read with bench_read() like a bench app's log.  `strays`: also
        the app's processes that are not (yet) in its cgroup - before the runner adopted them.
        `probe_host` ("host:port"): additionally a latency probe inside each app's cgroup
        (cf.linux.net_probe): connect time to that host and how late the probe wakes up."""
        from . import net_probe
        from .netstat import SockSampler
        self.net_stop()
        self.net_apps = list(ids)
        if not ids:
            return
        for a in ids if probe_host else []:
            log = net_probe.log_path(a)
            try:
                child = os.path.join(self.tree.ensure(a), net_probe.CHILD)
                os.makedirs(child, exist_ok=True)
                open(log, "w").close()
                self._bench_off[a + "#probe"] = 0
                self._probes[a] = subprocess.Popen(
                    [sys.executable, "-S", "-E", os.path.abspath(net_probe.__file__), "--log", log, "--host", probe_host,
                     "--every", str(probe_every_s), "--cgroup", child],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError as e:
                self.log.warn(f"{a}: latency probe not started: {type(e).__name__}: {e}")
        tree = self.tree

        def groups() -> dict:
            out = {}
            for a in self.net_apps:
                pids = self.pids(a)
                out[a] = {"cgroup": "/" + tree.subdir + "/" + a, "pids": pids + (self.stray_pids(a, pids) if strays else [])}
            return out

        self.net = SockSampler(groups, every_s=every_s, loopback=loopback, by=by, log=self.log.warn)
        self.net.start()

    def net_stop(self) -> None:
        if self.net is not None:
            self.net.stop()
            self.net.join(3.0)
            self.net = None
        from . import net_probe
        for a, p in list(self._probes.items()):
            p.terminate()
            try:
                p.wait(3.0)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(3.0)
            try:
                os.rmdir(os.path.join(self.tree.path(a), net_probe.CHILD))
            except OSError:
                pass
        self._probes.clear()

    def bench_read(self, id_: str) -> list[dict]:
        """New records since the last call: of a bench app's job log (cf.linux.bench_app), or of
        the socket sampler for a real app watched with net_start()."""
        if id_ in self.net_apps and self.cat[id_].kind != "bench":
            recs = self.net.read(id_) if self.net is not None else []
            if id_ in self._probes:
                from . import net_probe
                from .bench_app import read_new
                key = id_ + "#probe"
                more, self._bench_off[key] = read_new(net_probe.log_path(id_), self._bench_off.get(key, 0))
                recs += more
            return recs
        from .bench_app import log_path, read_new
        recs, self._bench_off[id_] = read_new(log_path(id_), self._bench_off.get(id_, 0))
        return recs

    def minimize(self, id_: str) -> None:
        w = self.window(id_)
        if w:
            x11.minimize(w["wid"])

    # ---------------------------------------------------------------- tiers
    def set_tier(self, id_: str, tier: str, m_fg_bytes: Optional[float] = None,
                 memory_high_bytes: Optional[int] = None) -> dict:
        if not self.tree.exists(id_):
            return {"tier": tier, "limits": {}, "reclaim": "no-cgroup"}
        res = apply_tier(self.tree, id_, tier, self.profile, m_fg_bytes, memory_high_bytes)
        self.tiers[id_] = tier
        return res

    def squeeze(self, id_: str, rounds: Optional[int] = None) -> dict:
        """Re-run the aggressive reclaim on an app that is already shrunk (profile.squeeze_every_s)."""
        if not self.tree.exists(id_):
            return {"how": "no-cgroup", "rounds": 0, "asked_bytes": 0, "before_bytes": 0, "after_bytes": 0}
        return squeeze(self.tree, id_, rounds if rounds is not None else self.profile.squeeze_rounds,
                       self.profile.reclaim_swappiness)

    def release_all(self) -> int:
        n = 0
        for leaf in self.tree.leaves():
            self.tree.clear_limits(leaf)
            self.tiers[leaf] = "fg"
            n += 1
        return n

    # ------------------------------------------------------------- sampling
    def sample(self, ids: list[str], with_windows: bool = True) -> dict:
        infos = x11.windows() if (with_windows and x11.have_display()) else []
        apps = {}
        for id_ in ids:
            pids = self.pids(id_)
            stray = self.stray_pids(id_, pids) if self.cat[id_].match else []
            if stray and self.cat[id_].is_mobile:
                # Android keeps forking new processes (services, :push) into the container's cgroup
                r = self.adopt(id_)
                if r["moved"]:
                    pids = self.pids(id_)
                    stray = [p for p in stray if p not in pids]
                # measure them even when they cannot be moved (the limit does not reach them, the
                # stray list in the record says so)
                pids = pids + [p for p in stray if p not in pids]
            plist = [pi for pi in (PR.proc_info(p) for p in pids) if pi is not None]
            win = self.window(id_, infos, pids) if infos else None
            own = None
            if self.cat[id_].kind == "bench" and self.cat[id_].match:
                rx = re.compile(self.cat[id_].match)
                own = [pi.pid for pi in plist if rx.search(pi.cmdline)]
            apps[id_] = AppSampleL(app=id_, procs=plist, cgroup=self.tree.stats(id_) if self.tree.exists(id_) else {},
                                   window=win["wid"] if win else None, tier=self.tiers.get(id_, "fg"),
                                   stray_pids=stray, state_pids=own)
        notifs = self.notify.snapshot() if self.notify else {}
        if self.mobile is not None and self.mobile.available and any(self.is_mobile(i) for i in ids):
            notifs = {**notifs, **self.mobile.notification_snapshot()}
        return {"ts": time.time(), "system": PR.system_sample(), "apps": apps, "notifications": notifs}

    @staticmethod
    def state_check(before: dict, after: dict) -> dict:
        """Compare two AppSampleL.to_dict(): processes gone, window gone, OOM kills."""
        lost = {}
        pids_b, pids_a = set(before.get("pids", [])), set(after.get("pids", []))
        stable_b = set(before.get("stable_pids", before.get("pids", [])))
        if pids_b and not pids_a:
            lost["all_processes_gone"] = True
        elif stable_b - pids_a:
            lost["processes_gone"] = sorted(stable_b - pids_a)
        if before.get("window") and not after.get("window"):
            lost["window_gone"] = True
        ev_b, ev_a = before.get("cg_events") or {}, after.get("cg_events") or {}
        for k in ("oom_kill", "oom"):
            if ev_a.get(k, 0) > ev_b.get(k, 0):
                lost[k] = ev_a[k] - ev_b.get(k, 0)
        return lost

    def notifications_for(self, id_: str, before: dict, after: dict) -> int:
        a = self.cat[id_]
        if a.is_mobile:
            key = f"mobile:{a.package}"
            return max(0, (after or {}).get(key, 0) - (before or {}).get(key, 0))
        if not self.notify:
            return 0
        d = NotifyCounter.delta(before, after)
        return NotifyCounter.count_for(d, list(a.notify_names) + [a.name, a.id])

    def close(self) -> None:
        self.net_stop()
        if self.notify:
            self.notify.stop()
        if self.pinger:
            self.pinger.close()
            self.pinger = None


def shell_quote(s: str) -> str:
    return shlex.quote(s)
