"""Scripted "work" on the foreground app and content-level state oracles.

The runner's state_check only sees processes, windows and OOM kills.  A WorkDriver adds the
user's point of view: while an app is in the foreground it performs a small amount of scripted
work (types a draft, scrolls a page, swipes a short-video feed), and right before the app is
switched away it reads back a compact description of the visible state (an *oracle*).  When the
app comes back - after a stretch in the `bg` or `shrunk` tier - the oracle is read again; a
mismatch means the throttled phase lost context (reset feed, cleared draft, relaunched activity)
and is recorded as `launch["work_state_ok"] = False` and `state_lost[app]["work_state"]`.

Two drivers, chosen per app kind:

* `X11WorkDriver` - desktop / oss / placeholder apps: input via `xdotool` (XTEST, to the active
  window), oracles: `clipboard` (ctrl+a ctrl+c -> md5 of the text), `title` (_NET_WM_NAME),
  `screen` (average hash of the window pixels via python-xlib, compared with a tolerance),
  `statefile` (the placeholder's /tmp/cf_placeholder_<id>.json: same pid, counter not reset).
* `AdbWorkDriver` - mobile builds in Waydroid: `input swipe` / `input text`, oracle `ui`
  (resumed activity + hash of the uiautomator view texts with digits removed) or `screen`
  (raw `screencap` average hash).

Per-app behaviour is a small dict (`LinuxApp.work`, overridable from the config):
    {"kind": edit|draft|browse|view|swipe|none, "oracle": clipboard|title|screen|statefile|ui|none,
     "text": "...", "scroll": 10, "swipes": 3, "tolerance": 12}
Defaults come from `default_spec()` (by id, then category / kind).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
import time
from typing import Optional

from . import x11
from .apps import LinuxApp

WORK_KINDS = ("edit", "draft", "browse", "view", "swipe", "none")
ORACLES = ("clipboard", "title", "screen", "statefile", "ui", "none")
SCREEN_GRID = 16                     # 16 x 16 cells -> 256-bit average hash
SCREEN_TOLERANCE_BITS = 12           # <= this many differing bits still counts as "same screen"

# ------------------------------------------------------------------ default per-app behaviour
_BY_ID = {
    # download: downloads continue by themselves (write_bytes is measured); only check the window
    "baidunetdisk": {"kind": "none", "oracle": "title"},
    # browsers / video sites: scroll the page; the title identifies the page, pixels change (video)
    "firefox": {"kind": "browse", "oracle": "title", "scroll": 10},
    "chrome": {"kind": "browse", "oracle": "title", "scroll": 10},
    "chromium": {"kind": "browse", "oracle": "title", "scroll": 10},
    "edge": {"kind": "browse", "oracle": "title", "scroll": 10},
    "tencent_video": {"kind": "browse", "oracle": "title", "scroll": 6},
    "bilibili": {"kind": "browse", "oracle": "title", "scroll": 6},
    "xunlei": {"kind": "browse", "oracle": "title", "scroll": 4},          # scroll the task list; the task keeps writing
    "iqiyi": {"kind": "browse", "oracle": "title", "scroll": 6},           # the episode page; title = the series
    "douyin": {"kind": "view", "oracle": "title", "scroll": 1},            # one wheel notch = next video in the feed
    "netease_music": {"kind": "browse", "oracle": "title", "scroll": 6},   # playlist page; the song keeps playing
    # online docs / meetings
    "shimo": {"kind": "edit", "oracle": "clipboard"},
    "tencent_docs": {"kind": "edit", "oracle": "clipboard"},
    "wemeet": {"kind": "none", "oracle": "title"},                         # resident only, never inside a meeting
    "feishu": {"kind": "draft", "oracle": "clipboard"},
    "wecom": {"kind": "draft", "oracle": "clipboard"},
    # chat: type an unsent draft into the focused input box, read it back
    "wechat": {"kind": "draft", "oracle": "clipboard"},
    "qq": {"kind": "draft", "oracle": "clipboard"},
    "dingtalk": {"kind": "draft", "oracle": "clipboard"},
    # editors: type text, read the whole document back
    "wps": {"kind": "edit", "oracle": "clipboard"},
    # canvases (CAD / image / video editors): zoom or pan with the wheel, compare the rendered view
    "zwcad": {"kind": "view", "oracle": "screen", "scroll": 3},
    "photosir": {"kind": "view", "oracle": "screen", "scroll": 3},
    "kdenlive": {"kind": "view", "oracle": "screen", "scroll": 3},
    "librecad": {"kind": "view", "oracle": "screen", "scroll": 3},
    "gimp": {"kind": "view", "oracle": "screen", "scroll": 3},
    "xiaoyi": {"kind": "none", "oracle": "title"},
}
_BY_CATEGORY = {
    "download": {"kind": "browse", "oracle": "title", "scroll": 8},
    "push": {"kind": "draft", "oracle": "clipboard"},
    "compute": {"kind": "edit", "oracle": "clipboard"},
}
# mobile builds: short-video feeds are swiped, chat apps get a draft, editors are scrolled; the
# oracle is always the resumed activity + the visible texts
_MOBILE = {
    "download": {"kind": "swipe", "oracle": "ui", "swipes": 3},
    "push": {"kind": "draft", "oracle": "ui"},
    "compute": {"kind": "view", "oracle": "ui", "scroll": 2},
}


def default_spec(a: LinuxApp) -> dict:
    """What to do / what to read back for this app when nothing is configured."""
    if a.kind == "placeholder":
        spec = {"kind": "none", "oracle": "statefile"}
    elif a.kind == "mobile":
        spec = dict(_MOBILE.get(a.category) or {"kind": "swipe", "oracle": "ui", "swipes": 2})
    else:
        base = a.id[:-len("_mobile")] if a.id.endswith("_mobile") else a.id
        spec = dict(_BY_ID.get(base) or _BY_CATEGORY.get(a.category) or {"kind": "none", "oracle": "title"})
    if a.work:
        spec.update(a.work)
    return spec


def work_text(app_id: str, t: int) -> str:
    """The marker typed by `edit` / `draft`: unique per step, so a stale clipboard is detected."""
    return f"cf-work {app_id} t{t} 草稿未保存 "


# ------------------------------------------------------------------ hashing helpers (pure python)
def average_hash(width: int, height: int, data: bytes, bpp: int = 4, grid: int = SCREEN_GRID) -> str:
    """Average hash of a raw packed image (bpp bytes per pixel, row-major).  Each grid cell is
    represented by the mean luminance of a few sampled pixels; bit = cell brighter than the
    global mean.  Robust to a blinking cursor, sensitive to scrolling / a reset view."""
    if width <= 0 or height <= 0 or len(data) < width * height * bpp:
        return ""
    stride = width * bpp
    cells = []
    samples = 6                                            # 6 x 6 samples per cell
    cw, ch = width / grid, height / grid
    for gy in range(grid):
        for gx in range(grid):
            acc = 0
            for sy in range(samples):
                y = min(height - 1, int((gy + (sy + 0.5) / samples) * ch))
                row = y * stride
                for sx in range(samples):
                    x = min(width - 1, int((gx + (sx + 0.5) / samples) * cw))
                    o = row + x * bpp
                    acc += data[o] + data[o + 1] + data[o + 2]
            cells.append(acc)
    mean = sum(cells) / len(cells)
    bits = 0
    for c in cells:
        bits = (bits << 1) | (1 if c > mean else 0)
    return f"{bits:0{grid * grid // 4}x}"


def hamming(a: str, b: str) -> int:
    if not a or not b or len(a) != len(b):
        return 10 ** 6
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def ui_text_hash(xml: str) -> str:
    """Hash of the texts / content descriptions in a uiautomator dump, digits removed (view
    counts, timestamps and like counters change while the content stays)."""
    vals = set()
    for m in re.finditer(r'(?:text|content-desc)="([^"]*)"', xml):
        v = re.sub(r"\d+", "#", m.group(1)).strip()
        if v:
            vals.add(v)
    return hashlib.md5("\n".join(sorted(vals)).encode("utf-8")).hexdigest()[:16] if vals else ""


def parse_wm_size(text: str) -> tuple[int, int]:
    m = re.search(r"(\d+)x(\d+)", text or "")
    return (int(m.group(1)), int(m.group(2))) if m else (1080, 1920)


# ------------------------------------------------------------------ comparison
def compare(before: Optional[dict], after: Optional[dict], tolerance: int = SCREEN_TOLERANCE_BITS) -> Optional[bool]:
    """True = same state, False = state lost, None = cannot tell (oracle unavailable on one side)."""
    if not before or not after or before.get("oracle") != after.get("oracle"):
        return None
    kind = before["oracle"]
    if before.get("value") is None or after.get("value") is None:
        return None
    if kind == "screen":
        return hamming(before["value"], after["value"]) <= tolerance
    if kind == "statefile":
        b, a = before["value"], after["value"]
        return b.get("pid") == a.get("pid") and (a.get("counter") or 0) >= (b.get("counter") or 0)
    if kind == "ui":
        return before["value"] == after["value"]
    return before["value"] == after["value"]


# ------------------------------------------------------------------ drivers
class X11WorkDriver:
    """xdotool input to the active window + python-xlib / xclip oracles."""

    def __init__(self, dev, log=None):
        self.dev = dev
        self.log = log
        self._disp = None
        self.err = ""

    # -- helpers
    def _display(self):
        if self._disp is None:
            try:
                from Xlib import display
                self._disp = display.Display()
            except Exception as e:   # noqa: BLE001
                self.err = f"{type(e).__name__}: {e}"
                self._disp = False
        return self._disp or None

    @staticmethod
    def _key(*keys: str) -> None:
        subprocess.run(["xdotool", "key", "--clearmodifiers", "--delay", "60", *keys],
                       capture_output=True, timeout=15)

    @staticmethod
    def _type(text: str) -> None:
        subprocess.run(["xdotool", "type", "--clearmodifiers", "--delay", "25", "--", text],
                       capture_output=True, timeout=60)

    @staticmethod
    def _scroll(n: int) -> None:
        if n:
            subprocess.run(["xdotool", "click", "--delay", "80", "--repeat", str(abs(n)), "5" if n > 0 else "4"],
                           capture_output=True, timeout=30)

    def _active(self, wid: Optional[int]) -> bool:
        return bool(wid) and x11.active_window() == wid

    def window_hash(self, wid: int) -> Optional[str]:
        d = self._display()
        if d is None:
            return None
        try:
            from Xlib import X
            win = d.create_resource_object("window", wid)
            g = win.get_geometry()
            w, h = g.width, g.height
            if g.depth < 24:
                self.err = f"depth {g.depth} not supported"
                return None
            img = win.get_image(0, 0, w, h, X.ZPixmap, 0xFFFFFFFF)   # client window only (no WM frame)
            return average_hash(w, h, img.data, bpp=4)                 # depth 24/32 -> 32 bpp in ZPixmap
        except Exception as e:   # noqa: BLE001 - unmapped / off-screen window
            self.err = f"{type(e).__name__}: {e}"
            return None

    def clipboard_md5(self) -> Optional[str]:
        try:
            p = subprocess.run(["xclip", "-o", "-selection", "clipboard"], capture_output=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired) as e:
            self.err = f"xclip: {e}"
            return None
        if p.returncode != 0:
            return None
        return f"{len(p.stdout)}:{hashlib.md5(p.stdout).hexdigest()[:16]}"

    @staticmethod
    def statefile_of(a: LinuxApp) -> str:
        m = re.search(r"--state-file\s+(\S+)", a.cmd or "")
        if m:
            return m.group(1).strip("'\"")
        return os.path.join(tempfile.gettempdir(), f"cf_placeholder_{a.id}.json")

    # -- interface
    def work(self, a: LinuxApp, wid: Optional[int], seconds: float, t: int, spec: dict) -> dict:
        t0 = time.monotonic()
        kind = spec.get("kind", "none")
        rec = {"kind": kind, "status": "ok", "elapsed_ms": 0}
        if kind == "none":
            rec["status"] = "skipped"
            return rec
        if not self._active(wid):
            rec["status"] = "not-active"
            return rec
        try:
            if kind in ("edit", "draft"):
                self._type(spec.get("text") or work_text(a.id, t))
                if kind == "edit":
                    self._key("Return")
            elif kind in ("browse", "view"):
                self._scroll(int(spec.get("scroll", 8)))
                if kind == "browse":
                    time.sleep(0.4)
                    self._scroll(-max(1, int(spec.get("scroll", 8)) // 4))   # back up a little: a real reader
            else:
                rec["status"] = f"unknown-kind:{kind}"
        except (OSError, subprocess.TimeoutExpired) as e:
            rec["status"] = f"error: {e}"
        rec["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
        left = seconds - (time.monotonic() - t0)
        if left > 0:
            time.sleep(left)
        return rec

    def oracle(self, a: LinuxApp, wid: Optional[int], spec: dict) -> Optional[dict]:
        o = spec.get("oracle", "none")
        if o == "none":
            return None
        out = {"oracle": o, "value": None, "ts": time.time()}
        if o == "statefile":
            try:
                with open(self.statefile_of(a)) as f:
                    st = json.load(f)
                out["value"] = {"pid": st.get("pid"), "counter": st.get("counter")}
            except (OSError, ValueError) as e:
                out["error"] = str(e)[:80]
            return out
        if not wid:
            out["error"] = "no-window"
            return out
        if o == "title":
            out["value"] = x11.window_info(wid).get("name")
        elif o == "screen":
            out["value"] = self.window_hash(wid)
            if out["value"] is None:
                out["error"] = self.err[:80]
        elif o == "clipboard":
            if not self._active(wid):
                out["error"] = "not-active"
                return out
            self._key("ctrl+a", "ctrl+c")
            time.sleep(0.3)
            out["value"] = self.clipboard_md5()
            self._key("End")                      # drop the selection so the next typing appends
            if out["value"] is None:
                out["error"] = self.err[:80] or "clipboard-unavailable"
        else:
            out["error"] = f"unknown-oracle:{o}"
        return out


class AdbWorkDriver:
    """`input` events and uiautomator / screencap oracles through the mobile runtime."""

    def __init__(self, dev, log=None):
        self.dev = dev
        self.mobile = getattr(dev, "mobile", None)
        self.log = log
        self._size: Optional[tuple[int, int]] = None
        self.err = ""

    def _sh(self, cmd: str, timeout: float = 30) -> str:
        return self.mobile.shell(cmd, timeout=timeout) if self.mobile is not None else ""

    def size(self) -> tuple[int, int]:
        if self._size is None:
            self._size = parse_wm_size(self._sh("wm size"))
        return self._size

    def screen_hash(self) -> Optional[str]:
        adb = getattr(self.mobile, "adb", None)
        if adb is None:
            return None
        try:
            p = subprocess.run(adb._base() + ["exec-out", "screencap"], capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as e:
            self.err = str(e)
            return None
        raw = p.stdout
        if len(raw) < 16:
            return None
        w = int.from_bytes(raw[0:4], "little"); h = int.from_bytes(raw[4:8], "little")
        hdr = 16 if len(raw) >= 16 + w * h * 4 else 12
        return average_hash(w, h, raw[hdr:], bpp=4)

    # -- interface
    def work(self, a: LinuxApp, wid: Optional[int], seconds: float, t: int, spec: dict) -> dict:
        t0 = time.monotonic()
        kind = spec.get("kind", "none")
        rec = {"kind": kind, "status": "ok", "elapsed_ms": 0}
        if kind == "none":
            rec["status"] = "skipped"
            return rec
        if self.mobile is None or not self.mobile.available:
            rec["status"] = "no-runtime"
            return rec
        fg = self.mobile.foreground_pkg()
        if fg and a.package and fg != a.package:
            rec["status"] = f"not-foreground:{fg}"
            return rec
        w, h = self.size()
        if kind == "swipe":                       # short-video feed: swipe up = next item
            n = int(spec.get("swipes", 2))
            for _ in range(n):
                self._sh(f"input swipe {w // 2} {int(h * 0.75)} {w // 2} {int(h * 0.25)} 250")
                time.sleep(max(0.3, (seconds * 0.8) / max(1, n)))
        elif kind in ("browse", "view"):          # scroll a list a few times
            for _ in range(int(spec.get("scroll", 3))):
                self._sh(f"input swipe {w // 2} {int(h * 0.7)} {w // 2} {int(h * 0.4)} 300")
                time.sleep(0.4)
        elif kind in ("edit", "draft"):
            self._sh("input text " + shlex.quote((spec.get("text") or work_text(a.id, t)).replace(" ", "%s")))
        else:
            rec["status"] = f"unknown-kind:{kind}"
        rec["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
        left = seconds - (time.monotonic() - t0)
        if left > 0:
            time.sleep(left)
        return rec

    def oracle(self, a: LinuxApp, wid: Optional[int], spec: dict) -> Optional[dict]:
        o = spec.get("oracle", "none")
        if o == "none":
            return None
        out = {"oracle": o, "value": None, "ts": time.time()}
        if self.mobile is None or not self.mobile.available:
            out["error"] = "no-runtime"
            return out
        act = self._sh("dumpsys activity activities | grep -E 'topResumedActivity|mResumedActivity|ResumedActivity' | head -1")
        m = re.search(r"([\w.]+/[\w.$]+)", act or "")
        activity = m.group(1) if m else None
        if o == "ui":
            xml = self._sh("uiautomator dump /sdcard/cf_ui.xml >/dev/null 2>&1; cat /sdcard/cf_ui.xml", timeout=40)
            h = ui_text_hash(xml)
            out["value"] = f"{activity}|{h}" if (activity or h) else None
            if out["value"] is None:
                out["error"] = "no-activity-no-ui"
        elif o == "screen":
            out["value"] = self.screen_hash()
            out["activity"] = activity
            if out["value"] is None:
                out["error"] = self.err[:80] or "screencap-unavailable"
        elif o == "title":
            out["value"] = activity
            if activity is None:
                out["error"] = "no-activity"
        else:
            out["error"] = f"unknown-oracle:{o}"
        return out


class WorkDriver:
    """Dispatches per app kind; keeps the per-app spec (catalogue default + config overrides)."""

    def __init__(self, dev, cfg: Optional[dict] = None, log=None):
        self.dev = dev
        self.cfg = cfg or {}
        self.x11 = X11WorkDriver(dev, log)
        self.adb = AdbWorkDriver(dev, log)
        self.overrides = dict(self.cfg.get("work_overrides") or {})
        self.tolerance = int(self.cfg.get("work_screen_tolerance") or SCREEN_TOLERANCE_BITS)

    def spec(self, a: LinuxApp) -> dict:
        s = default_spec(a)
        s.update(self.overrides.get(a.id) or {})
        return s

    def _driver(self, a: LinuxApp):
        return self.adb if a.is_mobile else self.x11

    def work(self, app_id: str, wid: Optional[int], seconds: float, t: int) -> dict:
        a = self.dev.cat[app_id]
        spec = self.spec(a)
        rec = self._driver(a).work(a, wid, seconds, t, spec)
        rec["oracle"] = spec.get("oracle", "none")
        return rec

    def oracle(self, app_id: str, wid: Optional[int]) -> Optional[dict]:
        a = self.dev.cat[app_id]
        return self._driver(a).oracle(a, wid, self.spec(a))

    def compare(self, before: Optional[dict], after: Optional[dict], app_id: Optional[str] = None) -> Optional[bool]:
        tol = self.tolerance
        if app_id is not None:
            tol = int(self.spec(self.dev.cat[app_id]).get("tolerance", tol))
        return compare(before, after, tol)

    def describe(self, apps: list[str], suffix: str = "") -> str:
        rows = []
        for p in apps:
            s = self.spec(self.dev.cat[p])
            rows.append(f"{p:16s} {s.get('kind', 'none'):7s} -> {s.get('oracle', 'none')}{suffix}")
        return "\n".join(rows)


def make_work_driver(dev, cfg: Optional[dict] = None, log=None) -> WorkDriver:
    return WorkDriver(dev, cfg, log)
