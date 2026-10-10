"""X11 window handling: discovery, focus switching and event-loop responsiveness.

Works on an Xorg session (Ubuntu 22.04: choose "Ubuntu on Xorg" at the login screen) or on
XWayland-only setups for XWayland clients.  Tools: xdotool, xprop (apt install xdotool x11-utils)
and python3-xlib for _NET_WM_PING.

"Resume latency" of a background window on Linux has no `am start -W`; we measure

    activate_ms   xdotool windowactivate --sync: until the WM reports the window as _NET_ACTIVE_WINDOW
    ping_ms       _NET_WM_PING round trip right after activation: the time the app's main loop needs
                  to get scheduled and answer (a throttled / swapped-out app answers late; this is
                  the Linux analogue of Android's ANR ping)
    total_time_ms activate_ms + ping_ms (the user-visible "it reacts again" time)
"""
from __future__ import annotations

import os
import re
import select
import shutil
import subprocess
import time
from typing import Iterable, Optional

from . import procs

_XPROP_LINE = re.compile(r"^([A-Za-z0-9_]+)\(([A-Z_0-9]+)\)(?::|\s*=)\s*(.*)$")


def have_display() -> bool:
    return bool(os.environ.get("DISPLAY"))


def session_type() -> str:
    return os.environ.get("XDG_SESSION_TYPE", "") or ("x11" if have_display() else "")


def tools_missing() -> list[str]:
    return [t for t in ("xdotool", "xprop") if shutil.which(t) is None]


def _run(cmd: list[str], timeout: float = 15) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return p.stdout


def xdotool(*args: str, timeout: float = 15) -> str:
    return _run(["xdotool", *args], timeout=timeout)


# ------------------------------------------------------------------ parsing
def parse_xprop(text: str) -> dict:
    """xprop output -> {pid, wm_class: [instance, class], name, protocols: [...], state: [...], type, desktop}."""
    out: dict = {"pid": None, "wm_class": [], "name": "", "protocols": [], "state": [], "type": "", "desktop": None}
    for line in text.splitlines():
        m = _XPROP_LINE.match(line.strip())
        if not m:
            continue
        key, _, val = m.groups()
        val = val.strip()
        if key == "_NET_WM_PID":
            out["pid"] = int(val) if val.isdigit() else None
        elif key == "WM_CLASS":
            out["wm_class"] = re.findall(r'"((?:[^"\\]|\\.)*)"', val)
        elif key in ("_NET_WM_NAME", "WM_NAME") and not out["name"]:
            m2 = re.match(r'"(.*)"$', val)
            out["name"] = m2.group(1) if m2 else val
        elif key == "WM_PROTOCOLS":
            out["protocols"] = [p.strip() for p in val.replace("protocols", "").split(",") if p.strip()]
        elif key == "_NET_WM_STATE":
            out["state"] = [p.strip() for p in val.split(",") if p.strip()]
        elif key == "_NET_WM_WINDOW_TYPE":
            out["type"] = val.split(",")[0].strip()
        elif key == "_NET_WM_DESKTOP":
            out["desktop"] = int(val) if val.lstrip("-").isdigit() else None
    return out


def parse_window_ids(text: str) -> list[int]:
    """'_NET_CLIENT_LIST(WINDOW): window id # 0x1400003, 0x1600004' or one id per line."""
    ids = []
    for tok in re.findall(r"0x[0-9a-fA-F]+|\b\d+\b", text.split("#", 1)[-1] if "#" in text else text):
        try:
            ids.append(int(tok, 16) if tok.startswith("0x") else int(tok))
        except ValueError:
            pass
    return ids


# ---------------------------------------------------------------- queries
def window_info(wid: int) -> dict:
    txt = _run(["xprop", "-id", str(wid), "_NET_WM_PID", "WM_CLASS", "_NET_WM_NAME", "WM_NAME",
                "WM_PROTOCOLS", "_NET_WM_STATE", "_NET_WM_WINDOW_TYPE", "_NET_WM_DESKTOP"])
    info = parse_xprop(txt)
    info["wid"] = wid
    return info


def client_list() -> list[int]:
    """Top-level managed windows (EWMH _NET_CLIENT_LIST); falls back to xdotool search."""
    txt = _run(["xprop", "-root", "_NET_CLIENT_LIST"])
    ids = parse_window_ids(txt) if "WINDOW" in txt else []
    if not ids:
        ids = [int(x) for x in xdotool("search", "--onlyvisible", "--name", ".").split() if x.isdigit()]
    return ids


def windows(with_cmdline: bool = False) -> list[dict]:
    out = []
    for wid in client_list():
        info = window_info(wid)
        if with_cmdline and info.get("pid"):
            info["cmdline"] = procs.cmdline(info["pid"])
        out.append(info)
    return out


def search(pid: Optional[int] = None, wm_class: Optional[str] = None, name: Optional[str] = None,
           onlyvisible: bool = True) -> list[int]:
    args = ["search"]
    if onlyvisible:
        args.append("--onlyvisible")
    if pid is not None:
        args += ["--pid", str(pid)]
    if wm_class:
        args += ["--class", wm_class]
    if name:
        args += ["--name", name]
    return [int(x) for x in xdotool(*args).split() if x.isdigit()]


def active_window() -> Optional[int]:
    txt = xdotool("getactivewindow").strip()
    return int(txt) if txt.isdigit() else None


def windows_for(pids: Iterable[int], class_rx: Optional[str] = None, name_rx: Optional[str] = None,
                infos: Optional[list[dict]] = None) -> list[dict]:
    """Windows that belong to the app: _NET_WM_PID in pids, or WM_CLASS / title matching the regexes.
    Normal, non-hidden windows first, so the main window wins over tray popups."""
    pids = set(pids)
    crx = re.compile(class_rx, re.I) if class_rx else None
    nrx = re.compile(name_rx, re.I) if name_rx else None
    hits = []
    for info in (infos if infos is not None else windows()):
        own = info.get("pid") in pids
        by_class = bool(crx and any(crx.search(c) for c in info.get("wm_class", [])))
        by_name = bool(nrx and nrx.search(info.get("name", "")))
        if own or by_class or by_name:
            hits.append(info)

    def rank(i: dict) -> tuple:
        normal = i.get("type", "") in ("", "_NET_WM_WINDOW_TYPE_NORMAL")
        hidden = "_NET_WM_STATE_HIDDEN" in i.get("state", [])
        skip = "_NET_WM_STATE_SKIP_TASKBAR" in i.get("state", [])
        return (not normal, skip, hidden, -(i.get("pid") in pids))
    return sorted(hits, key=rank)


# ---------------------------------------------------------------- actions
def activate(wid: int, timeout: float = 10.0) -> dict:
    """Bring a window to the foreground. Returns {ok, activate_ms}."""
    t0 = time.monotonic()
    try:
        subprocess.run(["xdotool", "windowactivate", "--sync", str(wid)], capture_output=True, text=True,
                       timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "activate_ms": int((time.monotonic() - t0) * 1000), "error": "timeout"}
    except OSError as e:
        return {"ok": False, "activate_ms": None, "error": str(e)}
    # --sync returns when the WM switched; confirm (some WMs ignore the request when focus stealing
    # prevention kicks in - then the window is only raised)
    deadline = time.monotonic() + max(0.5, timeout - (time.monotonic() - t0))
    while time.monotonic() < deadline:
        if active_window() == wid:
            return {"ok": True, "activate_ms": int((time.monotonic() - t0) * 1000)}
        time.sleep(0.02)
    return {"ok": False, "activate_ms": int((time.monotonic() - t0) * 1000), "error": "not-active"}


def minimize(wid: int) -> None:
    xdotool("windowminimize", str(wid))


def close(wid: int) -> None:
    xdotool("windowclose", str(wid))


# ------------------------------------------------------------------- ping
class Pinger:
    """_NET_WM_PING round trip via python-xlib. One Display connection, reused across calls."""

    def __init__(self):
        self.d = None
        self.err = ""
        try:
            from Xlib import X, display   # noqa: F401
            self.d = display.Display()
            self.root = self.d.screen().root
            self.WM_PROTOCOLS = self.d.intern_atom("WM_PROTOCOLS")
            self.NET_WM_PING = self.d.intern_atom("_NET_WM_PING")
            self.root.change_attributes(event_mask=X.SubstructureNotifyMask)
            self.d.flush()
        except Exception as e:   # noqa: BLE001 - no display / no python-xlib
            self.d = None
            self.err = f"{type(e).__name__}: {e}"

    @property
    def available(self) -> bool:
        return self.d is not None

    def supports(self, wid: int) -> bool:
        if not self.d:
            return False
        try:
            win = self.d.create_resource_object("window", wid)
            prot = win.get_wm_protocols()
            return self.NET_WM_PING in (prot or [])
        except Exception:   # noqa: BLE001
            return False

    def ping(self, wid: int, timeout: float = 10.0) -> Optional[float]:
        """Milliseconds until the client answers, None when unsupported or timed out."""
        if not self.d:
            return None
        from Xlib import X
        from Xlib.protocol import event as xevent
        try:
            win = self.d.create_resource_object("window", wid)
            if not self.supports(wid):
                return None
            # drain stale events
            while self.d.pending_events():
                self.d.next_event()
            ts = int(time.monotonic() * 1000) & 0x7FFFFFFF
            ev = xevent.ClientMessage(window=win, client_type=self.WM_PROTOCOLS,
                                      data=(32, [self.NET_WM_PING, ts, wid, 0, 0]))
            t0 = time.monotonic()
            win.send_event(ev, event_mask=X.NoEventMask, propagate=False)
            self.d.flush()
            deadline = t0 + timeout
            while True:
                while self.d.pending_events():
                    e = self.d.next_event()
                    if e.type == X.ClientMessage and getattr(e, "client_type", None) == self.WM_PROTOCOLS:
                        fmt, data = e.data
                        if fmt == 32 and data[0] == self.NET_WM_PING and data[1] == ts:
                            return round((time.monotonic() - t0) * 1000, 2)
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                r, _, _ = select.select([self.d.fileno()], [], [], min(left, 0.25))
                if not r:
                    continue
        except Exception as e:   # noqa: BLE001 - window vanished, connection error
            self.err = f"{type(e).__name__}: {e}"
            return None

    def close(self) -> None:
        if self.d:
            try:
                self.d.close()
            except Exception:   # noqa: BLE001
                pass
            self.d = None


def wm_name() -> str:
    """Name of the running window manager (_NET_SUPPORTING_WM_CHECK -> _NET_WM_NAME)."""
    txt = _run(["xprop", "-root", "_NET_SUPPORTING_WM_CHECK"])
    ids = parse_window_ids(txt) if "WINDOW" in txt else []
    if not ids:
        return ""
    return parse_xprop(_run(["xprop", "-id", str(ids[0]), "_NET_WM_NAME"])).get("name", "")


def discover_table(cat: Optional[dict] = None, leaf_of=None) -> str:
    """For filling the config: every managed window with pid / WM_CLASS / title / command, plus the
    cgroup leaf the process lives in and the catalogue id whose wm_class / name_rx / pid matches."""
    rows = [("WID", "PID", "WM_CLASS", "TITLE", "CGROUP", "APP", "CMDLINE")]
    for w in windows(with_cmdline=True):
        pid = w.get("pid")
        leaf = (leaf_of(pid) if (leaf_of and pid) else None) or "-"
        app = leaf if leaf in (cat or {}) else "-"
        if app == "-":
            for id_, a in (cat or {}).items():
                if windows_for([], a.wm_class, a.name_rx or None, [w]):
                    app = id_
                    break
        rows.append((hex(w["wid"]), str(pid or "-"), ",".join(w.get("wm_class", [])) or "-",
                     (w.get("name") or "-")[:32], leaf, app, (w.get("cmdline") or "-")[:60]))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(c.ljust(widths[i]) for i, c in enumerate(r)) for r in rows)
