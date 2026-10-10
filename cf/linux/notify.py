"""Count desktop notifications per application (push category: 微信 / QQ / 钉钉).

`dbus-monitor` on the session bus prints every org.freedesktop.Notifications.Notify call:

    method call ... interface=org.freedesktop.Notifications; member=Notify
       string "微信"          <- app_name
       uint32 0               <- replaces_id
       string ""              <- app_icon
       string "张三"          <- summary
       string "在吗"          <- body
       ...

The counter runs in a background thread; the runner takes a snapshot before and after each dwell.
Works with GNOME Shell's notification server as well as dunst / xfce4-notifyd.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time
from typing import Optional

_STR = re.compile(r'^\s*string "((?:[^"\\]|\\.)*)"')


class _Parser:
    """Feed dbus-monitor lines, get (app_name, summary) when a Notify call is complete."""

    def __init__(self):
        self.app: Optional[str] = None
        self.strings_seen = 0
        self.in_call = False

    def feed(self, line: str) -> Optional[tuple[str, str]]:
        if "member=Notify" in line and "method call" in line:
            self.in_call, self.app, self.strings_seen = True, None, 0
            return None
        if not self.in_call:
            return None
        m = _STR.match(line)
        if not m:
            return None
        self.strings_seen += 1
        if self.strings_seen == 1:            # app_name
            self.app = m.group(1) or "?"
        elif self.strings_seen == 3:          # app_icon is #2, summary is #3
            app, summary = self.app or "?", m.group(1)
            self.in_call, self.app, self.strings_seen = False, None, 0
            return app, summary
        return None


def parse_monitor_text(text: str) -> list[tuple[str, str]]:
    p = _Parser()
    out = []
    for line in text.splitlines():
        ev = p.feed(line)
        if ev:
            out.append(ev)
    return out


class NotifyCounter:
    def __init__(self, bus: str = "session"):
        self.counts: dict[str, int] = {}
        self.events: list[tuple[float, str, str]] = []     # (ts, app_name, summary)
        self.lock = threading.Lock()
        self.proc: Optional[subprocess.Popen] = None
        self.thread: Optional[threading.Thread] = None
        self.error = ""
        self.bus = bus

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> bool:
        if shutil.which("dbus-monitor") is None:
            self.error = "dbus-monitor not installed (apt install dbus)"
            return False
        try:
            self.proc = subprocess.Popen(
                ["dbus-monitor", f"--{self.bus}",
                 "type='method_call',interface='org.freedesktop.Notifications',member='Notify'"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        except OSError as e:
            self.error = str(e)
            return False
        self.thread = threading.Thread(target=self._reader, daemon=True)
        self.thread.start()
        time.sleep(0.3)
        if not self.running:
            self.error = (self.proc.stderr.read() if self.proc.stderr else "").strip()[:200] or "exited"
            self.proc = None
            return False
        return True

    def _reader(self) -> None:
        assert self.proc and self.proc.stdout
        p = _Parser()
        for line in self.proc.stdout:
            ev = p.feed(line)
            if ev:
                app, summary = ev
                with self.lock:
                    self.counts[app] = self.counts.get(app, 0) + 1
                    self.events.append((time.time(), app, summary[:80]))
                    if len(self.events) > 5000:
                        del self.events[:1000]

    def snapshot(self) -> dict[str, int]:
        with self.lock:
            return dict(self.counts)

    @staticmethod
    def delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
        return {k: v - before.get(k, 0) for k, v in after.items() if v - before.get(k, 0) > 0}

    @staticmethod
    def count_for(delta: dict[str, int], names: list[str]) -> int:
        """Notifications whose app_name equals / contains one of the app's known names."""
        low = [n.lower() for n in names if n]
        return sum(v for k, v in delta.items() if any(n == k.lower() or n in k.lower() for n in low))

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
