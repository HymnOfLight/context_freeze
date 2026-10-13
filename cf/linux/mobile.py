"""Mobile (Android) builds of the apps, run on the Ubuntu desktop inside Waydroid.

Why: some apps have no Ubuntu desktop build at all (剪映, 小艺), others may not be obtainable as an
arm64 deb.  The brief says: desktop version first, the app's own mobile version second - never a
different program.  Waydroid runs an Android (LineageOS) userspace in an LXC container on the
*host kernel*, so its app processes are ordinary host processes: we can put them into
/sys/fs/cgroup/cf/<app> and throttle them exactly like the desktop apps (CPU quota, memory.high,
swap), and sample them through /proc.  Nothing is emulated; arm64 APKs run natively on the arm64 VM.

What differs from a desktop app:

  window     all Android apps share one X11 window (the nested Weston that Waydroid draws into on an
             Xorg session, scripts/ubuntu/setup_waydroid.sh).  Switching *to* a mobile app =
             activate that window + `am start -W` inside Android; the switch latency is
             activate_ms + the TotalTime that `am start -W` reports (the same number the Android
             back-end of this repo measures), with LaunchState HOT / WARM / COLD from Android itself.
  processes  found on the host by cmdline (== package or package:service), re-adopted into the
             cgroup after every launch (zygote forks them into the container's cgroup, so moving
             them needs root: `sudo_adopt` + scripts/ubuntu/cf_adopt.sh in sudoers).
  notifs     Android notifications are counted with `dumpsys notification` (distinct keys seen),
             not over D-Bus.
  shell      adb to the Waydroid IP (`adb connect 192.168.240.112:5555`, no root needed); falls back
             to `sudo waydroid shell` when adb is not connected.

Config (`"mobile": {...}` in the experiment JSON):
    backend          waydroid (default) | adb (any Android reachable over adb, e.g. an emulator with
                     nested virtualisation - then host pids are invisible and per-app cgroups impossible)
    adb_serial       default: the IP `waydroid status` prints + :5555
    wm_class         regex of the Android display's X11 window (default: weston / Waydroid)
    wayland_display  WAYLAND_DISPLAY to start the session on when it is not running
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import time
from typing import Optional

from .. import parsers as P
from ..logging_util import as_logger
from . import procs as PR

DEFAULT_SERIAL = "192.168.240.112:5555"
_NOTIF_RX = re.compile(r"NotificationRecord\([^:]*:\s*pkg=(\S+).*?(?:key=(\S+?)(?::\s|\s)|id=(\S+)\s+tag=(\S+))", re.S)
_RESUMED_RX = re.compile(r"(?:topResumedActivity|mResumedActivity|ResumedActivity)[^\n]*?\s([\w.]+)/[\w.$]+")


def parse_waydroid_status(text: str) -> dict:
    out: dict = {}
    for line in text.splitlines():
        k, _, v = line.partition(":")
        if _:
            out[k.strip().lower().replace(" ", "_")] = v.strip()
    return out


def parse_dumpsys_notifications(text: str) -> dict[str, set[str]]:
    """pkg -> set of notification keys currently posted."""
    out: dict[str, set[str]] = {}
    for m in _NOTIF_RX.finditer(text):
        pkg = m.group(1)
        key = m.group(2) or f"{m.group(3)}|{m.group(4)}"
        out.setdefault(pkg, set()).add(key)
    return out


def parse_resumed_pkg(text: str) -> Optional[str]:
    m = _RESUMED_RX.search(text)
    return m.group(1) if m else None


class MobileRuntime:
    def __init__(self, cfg: Optional[dict] = None, log=None):
        cfg = dict(cfg or {})
        self.backend = cfg.get("backend", "waydroid")
        self.serial = cfg.get("adb_serial")
        self.wm_class = cfg.get("wm_class", r"^(weston|Waydroid|waydroid|org\.freedesktop\.Weston)")
        self.wayland_display = cfg.get("wayland_display", "waydroid")
        self.log = as_logger(log)
        self.adb = None
        self.error = ""
        self._installed: dict[str, bool] = {}
        self._activity: dict[str, str] = {}
        self._uid: dict[str, int] = {}
        self._seen_notif: dict[str, set[str]] = {}
        self.caps: dict = {}

    # ------------------------------------------------------------- transport
    def _waydroid_status(self) -> dict:
        if shutil.which("waydroid") is None:
            return {}
        try:
            p = subprocess.run(["waydroid", "status"], capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            return {}
        return parse_waydroid_status(p.stdout + p.stderr)

    def _connect_adb(self) -> bool:
        try:
            from ..adb import Adb, AdbError
        except ImportError:
            return False
        try:
            adb = Adb(self.serial)
        except AdbError as e:                     # no adb binary
            self.error = str(e)
            return False
        if self.serial and ":" in self.serial:
            try:
                adb.run("connect", self.serial, check=False, timeout=15)
            except (OSError, subprocess.TimeoutExpired):
                pass
        try:
            out = adb.shell("echo cf_ok", timeout=15)
        except (OSError, subprocess.TimeoutExpired, AdbError) as e:
            self.error = str(e)
            return False
        if "cf_ok" not in out:
            self.error = out.strip()[:120] or "adb shell failed"
            return False
        self.adb = adb
        return True

    def shell(self, cmd: str, timeout: float = 60) -> str:
        if self.adb is not None:
            return self.adb.shell(cmd, timeout=timeout)
        if self.backend == "waydroid" and shutil.which("waydroid"):
            try:
                p = subprocess.run(["sudo", "-n", "waydroid", "shell", "--", "sh", "-c", cmd],
                                   capture_output=True, text=True, timeout=timeout)
                return p.stdout + p.stderr
            except (OSError, subprocess.TimeoutExpired) as e:
                self.error = str(e)
        return ""

    @property
    def available(self) -> bool:
        return bool(self.caps.get("available"))

    def probe(self) -> dict:
        st = self._waydroid_status() if self.backend == "waydroid" else {}
        c: dict = {"backend": self.backend, "waydroid_installed": shutil.which("waydroid") is not None,
                   "session": st.get("session"), "container": st.get("container"), "ip": st.get("ip_address"),
                   "wayland_display": st.get("wayland_display")}
        if self.serial is None:
            self.serial = f"{st['ip_address']}:5555" if st.get("ip_address") else (
                DEFAULT_SERIAL if self.backend == "waydroid" else os.environ.get("ANDROID_SERIAL"))
        c["adb_serial"] = self.serial
        c["adb"] = self._connect_adb()
        c["adb_error"] = "" if c["adb"] else self.error
        if c["adb"] or (self.backend == "waydroid" and st.get("session") == "RUNNING"):
            c["abi"] = self.shell("getprop ro.product.cpu.abi").strip()
            c["android"] = self.shell("getprop ro.build.version.release").strip()
            c["available"] = True
        else:
            c["available"] = False
        # host-visible processes (LXC on the host kernel) are what makes per-app cgroups possible
        c["host_pids_visible"] = bool(PR.find_pids(r"^(zygote64|zygote|system_server)$", uid=0)) or \
            bool(PR.find_pids(r"^system_server$", uid=1000))
        c["per_app_cgroups"] = c["host_pids_visible"]
        self.caps = c
        return c

    def ensure_session(self, timeout: float = 90) -> bool:
        """Start the Waydroid session when it is not running (needs a Wayland display: the nested
        Weston from scripts/ubuntu/setup_waydroid.sh)."""
        if self.backend != "waydroid":
            return self.available
        st = self._waydroid_status()
        if st.get("session") == "RUNNING":
            return True
        env = dict(os.environ)
        env.setdefault("WAYLAND_DISPLAY", self.wayland_display)
        try:
            subprocess.Popen(["waydroid", "session", "start"], env=env, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as e:
            self.error = str(e)
            return False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(2)
            if self._waydroid_status().get("session") == "RUNNING":
                time.sleep(3)
                self._connect_adb()
                return True
        self.error = "waydroid session did not reach RUNNING (is weston / WAYLAND_DISPLAY up? see setup_waydroid.sh)"
        return False

    # --------------------------------------------------------------- packages
    def installed(self, pkg: str) -> bool:
        if pkg not in self._installed:
            out = self.shell(f"pm path {shlex.quote(pkg)}", timeout=30) if self.available else ""
            self._installed[pkg] = "package:" in out
        return self._installed[pkg]

    def uid_of(self, pkg: str) -> Optional[int]:
        if pkg in self._uid:
            return self._uid[pkg]
        out = self.shell(f"cmd package list packages -U {shlex.quote(pkg)}")
        m = re.search(rf"package:{re.escape(pkg)}\s+uid:(\d+)", out)
        uid = int(m.group(1)) if m else P.parse_uid_from_dumpsys(
            self.shell(f"dumpsys package {shlex.quote(pkg)} | grep -m1 userId="))
        if uid is not None:
            self._uid[pkg] = uid
        return uid

    def launcher_activity(self, pkg: str) -> Optional[str]:
        if pkg in self._activity:
            return self._activity[pkg]
        for _ in range(3):
            out = self.shell(f"cmd package resolve-activity --brief -a android.intent.action.MAIN "
                             f"-c android.intent.category.LAUNCHER {shlex.quote(pkg)}")
            comp = P.parse_resolve_activity(out)
            if comp and comp.startswith(pkg):
                self._activity[pkg] = comp
                return comp
            if "No activity found" in out or "does not exist" in out:
                return None
            time.sleep(1)
        return None

    # ----------------------------------------------------------------- state
    def host_pids(self, pkg: str) -> list[int]:
        """Host pids of the Android app (Waydroid: cmdline is the Android process name)."""
        rx = rf"^{re.escape(pkg)}(:|$)"
        hits = []
        for pid in PR.all_pids():
            if re.match(rx, PR.cmdline(pid)):
                hits.append(pid)
        return hits

    def foreground_pkg(self) -> Optional[str]:
        out = self.shell("dumpsys activity activities | grep -E 'topResumedActivity|mResumedActivity|ResumedActivity' | head -3")
        return parse_resumed_pkg(out)

    def launch(self, pkg: str, timeout: float = 120) -> dict:
        """`am start -W` the launcher activity; returns cf.parsers.parse_am_start() fields."""
        comp = self.launcher_activity(pkg)
        if not comp:
            return {"status": "no-activity", "launch_state": None, "total_time_ms": None, "component": None}
        cmd = (f"am start -W -a android.intent.action.MAIN -c android.intent.category.LAUNCHER -n {shlex.quote(comp)}")
        t0 = time.time()
        out = self.shell(cmd, timeout=timeout)
        if not out.strip():
            out = self.shell(cmd, timeout=timeout)
        res = P.parse_am_start(out)
        res["component"] = comp
        res["am_elapsed_ms"] = int((time.time() - t0) * 1000)
        return res

    def notification_snapshot(self) -> dict[str, int]:
        """Cumulative number of distinct notifications seen per package (grows monotonically)."""
        if not self.available:
            return {}
        now = parse_dumpsys_notifications(self.shell("dumpsys notification --noredact 2>/dev/null || dumpsys notification",
                                                     timeout=30))
        for pkg, keys in now.items():
            self._seen_notif.setdefault(pkg, set()).update(keys)
        return {f"mobile:{pkg}": len(keys) for pkg, keys in self._seen_notif.items()}

    def describe(self) -> str:
        c = self.caps
        if not c:
            return "mobile runtime: not probed"
        if not c.get("available"):
            return f"mobile runtime ({c.get('backend')}): unavailable - {c.get('adb_error') or 'waydroid session not running'}"
        return (f"mobile runtime: {c.get('backend')} android {c.get('android')} {c.get('abi')} via adb {c.get('adb_serial')}"
                + (" (host pids visible -> per-app cgroups)" if c.get("per_app_cgroups") else
                   " (host pids NOT visible -> limits cannot be applied per app)"))
