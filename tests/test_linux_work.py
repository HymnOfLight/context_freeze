"""cf.linux.work: per-app defaults, hashing / comparison (pure), the adb driver against a fake
mobile runtime, and - when Xvfb + xdotool are available - the X11 driver on a placeholder window
(title / statefile / screen oracles) and, with mousepad, the edit + clipboard round trip."""
import json
import os
import shutil
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import test_linux_x11  # noqa: E402
from test_linux_x11 import _xvfb_stack_available  # noqa: E402

xvfb = test_linux_x11.xvfb        # the Xvfb + WM fixture is shared with the x11 tests

from cf.linux import apps as A  # noqa: E402
from cf.linux import work as W  # noqa: E402
from cf.linux import x11  # noqa: E402
from cf.linux.apps import LinuxApp  # noqa: E402


# ------------------------------------------------------------------ defaults
def test_default_spec_by_id_category_and_kind():
    cat = A.CATALOG
    assert W.default_spec(cat["wps"]) == {"kind": "edit", "oracle": "clipboard"}
    assert W.default_spec(cat["wechat"]) == {"kind": "draft", "oracle": "clipboard"}
    assert W.default_spec(cat["bilibili"]) == {"kind": "browse", "oracle": "title", "scroll": 6}
    assert W.default_spec(cat["zwcad"])["oracle"] == "screen"
    assert W.default_spec(cat["baidunetdisk"]) == {"kind": "none", "oracle": "title"}
    # unknown id -> category default; unknown category -> harmless title check
    assert W.default_spec(LinuxApp("x", "x", "push", "true", "x", "x")) == {"kind": "draft", "oracle": "clipboard"}
    assert W.default_spec(LinuxApp("x", "x", "other", "true", "x", "x")) == {"kind": "none", "oracle": "title"}
    # placeholders are checked through their state file, mobile builds are swiped and checked by ui
    assert W.default_spec(A.placeholder_for(cat["wechat"])) == {"kind": "none", "oracle": "statefile"}
    m = W.default_spec(cat["bilibili_mobile"])
    assert m["kind"] == "swipe" and m["oracle"] == "ui" and m["swipes"] == 3
    # catalogue-level `work` and config overrides win, field by field
    a = LinuxApp("wps2", "x", "compute", "true", "x", "x", work={"oracle": "screen", "tolerance": 4})
    assert W.default_spec(a) == {"kind": "edit", "oracle": "screen", "tolerance": 4}
    assert A.with_overrides({"wps": {"work": {"kind": "none"}}})["wps"].work == {"kind": "none"}
    assert all(s["kind"] in W.WORK_KINDS and s["oracle"] in W.ORACLES for s in map(W.default_spec, cat.values()))


def test_work_text_is_unique_per_step():
    assert W.work_text("wps", 3) != W.work_text("wps", 4) and "wps" in W.work_text("wps", 3)


# ------------------------------------------------------------------ hashing / comparison
def _img(w, h, fn):
    out = bytearray()
    for y in range(h):
        for x in range(w):
            v = fn(x, y)
            out += bytes((v, v, v, 255))
    return bytes(out)


def test_average_hash_and_hamming():
    w, h = 64, 64
    left_bright = _img(w, h, lambda x, y: 220 if x < 32 else 20)
    assert len(W.average_hash(w, h, left_bright)) == 64                   # 256 bits as hex
    assert W.hamming(W.average_hash(w, h, left_bright), W.average_hash(w, h, left_bright)) == 0
    # a blinking cursor (a 2x8 patch) does not flip cells; a scrolled / inverted view does
    cursor = _img(w, h, lambda x, y: (20 if (10 <= x < 12 and 10 <= y < 18) else 220) if x < 32 else 20)
    assert W.hamming(W.average_hash(w, h, left_bright), W.average_hash(w, h, cursor)) <= W.SCREEN_TOLERANCE_BITS
    inverted = _img(w, h, lambda x, y: 20 if x < 32 else 220)
    assert W.hamming(W.average_hash(w, h, left_bright), W.average_hash(w, h, inverted)) == 256
    shifted = _img(w, h, lambda x, y: 220 if x < 40 else 20)
    assert 20 < W.hamming(W.average_hash(w, h, left_bright), W.average_hash(w, h, shifted)) < 256
    assert W.average_hash(0, 0, b"") == "" and W.average_hash(4, 4, b"\0" * 10) == ""
    assert W.hamming("", "ab") >= 10 ** 6 and W.hamming("ab", "abc") >= 10 ** 6


def test_compare_per_oracle():
    assert W.compare(None, {"oracle": "title", "value": "x"}) is None
    assert W.compare({"oracle": "title", "value": "x"}, {"oracle": "screen", "value": "x"}) is None
    assert W.compare({"oracle": "title", "value": "x"}, {"oracle": "title", "value": None}) is None
    assert W.compare({"oracle": "title", "value": "a - WPS"}, {"oracle": "title", "value": "a - WPS"}) is True
    assert W.compare({"oracle": "clipboard", "value": "10:ab"}, {"oracle": "clipboard", "value": "0:cd"}) is False
    sf = lambda pid, c: {"oracle": "statefile", "value": {"pid": pid, "counter": c}}  # noqa: E731
    assert W.compare(sf(10, 5), sf(10, 9)) is True            # progress continued
    assert W.compare(sf(10, 5), sf(10, 5)) is True            # stalled is not lost
    assert W.compare(sf(10, 5), sf(10, 1)) is False           # counter reset -> restarted
    assert W.compare(sf(10, 5), sf(11, 9)) is False           # new pid
    h0 = "0" * 64
    h1 = "0" * 63 + "f"                                       # 4 bits differ
    assert W.compare({"oracle": "screen", "value": h0}, {"oracle": "screen", "value": h1}) is True
    assert W.compare({"oracle": "screen", "value": h0}, {"oracle": "screen", "value": h1}, tolerance=3) is False


def test_ui_text_hash_ignores_counters_and_parse_wm_size():
    a = '<node text="点赞 1234" content-desc="评论 56"/><node text="标题"/>'
    b = '<node text="点赞 1240" content-desc="评论 57"/><node text="标题"/>'
    c = '<node text="点赞 1240" content-desc="评论 57"/><node text="另一个视频"/>'
    assert W.ui_text_hash(a) == W.ui_text_hash(b) != W.ui_text_hash(c) and W.ui_text_hash("") == ""
    assert W.parse_wm_size("Physical size: 1080x2340\n") == (1080, 2340)
    assert W.parse_wm_size("") == (1080, 1920)


# ------------------------------------------------------------------ adb driver on a fake runtime
class _FakeMobile:
    available = True
    adb = None

    def __init__(self):
        self.cmds = []
        self.fg = "tv.danmaku.bili"
        self.activity = "tv.danmaku.bili/.MainActivityV2"
        self.ui = '<node text="推荐 12"/><node text="视频A"/>'

    def shell(self, cmd, timeout=30):
        self.cmds.append(cmd)
        if cmd == "wm size":
            return "Physical size: 720x1280"
        if "dumpsys activity" in cmd:
            return f"    mResumedActivity: ActivityRecord{{abc u0 {self.activity} t12}}"
        if "uiautomator" in cmd:
            return self.ui
        return ""

    def foreground_pkg(self):
        return self.fg


def test_adb_driver_swipes_and_ui_oracle():
    class Dev:
        cat = A.CATALOG
    dev = Dev()
    dev.mobile = _FakeMobile()
    drv = W.WorkDriver(dev, {"work_overrides": {"bilibili_mobile": {"swipes": 2}}})
    assert drv.spec(A.CATALOG["bilibili_mobile"])["swipes"] == 2
    t0 = time.monotonic()
    rec = drv.work("bilibili_mobile", None, 0.1, 3)
    assert rec["status"] == "ok" and rec["kind"] == "swipe" and rec["oracle"] == "ui"
    swipes = [c for c in dev.mobile.cmds if c.startswith("input swipe")]
    assert len(swipes) == 2 and swipes[0] == "input swipe 360 960 360 320 250" and time.monotonic() - t0 < 5
    o1 = drv.oracle("bilibili_mobile", None)
    assert o1["oracle"] == "ui" and o1["value"].startswith("tv.danmaku.bili/.MainActivityV2|")
    dev.mobile.ui = '<node text="推荐 99"/><node text="视频A"/>'          # counters change, content same
    assert drv.compare(o1, drv.oracle("bilibili_mobile", None), "bilibili_mobile") is True
    dev.mobile.activity = "tv.danmaku.bili/.SplashActivity"               # relaunched from scratch
    assert drv.compare(o1, drv.oracle("bilibili_mobile", None), "bilibili_mobile") is False
    dev.mobile.fg = "com.tencent.mm"                                       # not in front -> no input is sent
    assert drv.work("bilibili_mobile", None, 0.01, 4)["status"] == "not-foreground:com.tencent.mm"
    dev.mobile.available = False
    assert drv.oracle("bilibili_mobile", None)["error"] == "no-runtime"
    # draft on a mobile chat app goes through `input text`
    dev.mobile.available, dev.mobile.fg = True, "com.tencent.mm"
    assert drv.work("wechat_mobile", None, 0.01, 5)["kind"] == "draft"
    assert any(c.startswith("input text ") and "%s" in c for c in dev.mobile.cmds)
    assert "wechat_mobile" in drv.describe(["wechat_mobile"]) and "draft" in drv.describe(["wechat_mobile"])


# ------------------------------------------------------------------ X11 driver on Xvfb
def _wait_window(pid, app, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        hit = x11.windows_for([pid], class_rx=f"^cf-{app}$")
        if hit:
            return hit[0]["wid"]
        time.sleep(0.3)
    raise AssertionError("placeholder window did not appear")


@pytest.mark.skipif(not _xvfb_stack_available(), reason="needs Xvfb + a WM + xdotool/xprop + python3-xlib")
def test_x11_driver_oracles_on_placeholder(xvfb, tmp_path):
    env = dict(os.environ, PYTHONPATH=os.getcwd())
    sf = tmp_path / "ph.json"
    a = LinuxApp("ph", "占位", "compute", cmd=f"python3 -m cf.linux.placeholder_app --app ph --state-file {sf}",
                 match="placeholder_app.*--app ph", wm_class="^cf-ph$", kind="placeholder")
    p = subprocess.Popen([sys.executable, "-m", "cf.linux.placeholder_app", "--app", "ph", "--title", "占位",
                          "--cpu", "0.05", "--mem", "8", "--state-file", str(sf)],
                         env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wid = _wait_window(p.pid, "ph")
        assert x11.activate(wid, timeout=10)["ok"]

        class Dev:
            cat = {"ph": a}
        drv = W.WorkDriver(Dev(), {"work_overrides": {}})
        assert drv.spec(a) == {"kind": "none", "oracle": "statefile"}
        assert drv.work("ph", wid, 0.05, 1)["status"] == "skipped"
        # statefile: same pid, counter keeps increasing
        for _ in range(60):
            if sf.exists() and json.load(open(sf)).get("pid") == p.pid:
                break
            time.sleep(0.2)
        o1 = drv.oracle("ph", wid)
        assert o1["oracle"] == "statefile" and o1["value"]["pid"] == p.pid
        time.sleep(1.5)
        assert drv.compare(o1, drv.oracle("ph", wid), "ph") is True
        assert W.compare(o1, {"oracle": "statefile", "value": {"pid": p.pid + 1, "counter": 0}}) is False
        # title and screen oracles on the same window
        x = drv.x11
        t1 = x.oracle(a, wid, {"oracle": "title"})
        assert t1["value"] == "占位" and W.compare(t1, x.oracle(a, wid, {"oracle": "title"})) is True
        s1 = x.oracle(a, wid, {"oracle": "screen"})
        assert s1["value"] and len(s1["value"]) == 64, s1
        s2 = x.oracle(a, wid, {"oracle": "screen"})
        assert W.hamming(s1["value"], s2["value"]) <= 40        # only the counter digits change
        assert x.oracle(a, None, {"oracle": "title"})["error"] == "no-window"
        assert x.statefile_of(a) == str(sf)
        # view = wheel scroll; it must not fail on a window that ignores the wheel
        assert x.work(a, wid, 0.05, 2, {"kind": "view", "scroll": 2})["status"] == "ok"
    finally:
        p.terminate()
        p.wait(timeout=5)


@pytest.mark.skipif(not _xvfb_stack_available() or not shutil.which("mousepad") or not shutil.which("xclip"),
                    reason="needs the Xvfb stack + mousepad + xclip")
def test_x11_edit_and_clipboard_round_trip(xvfb, tmp_path):
    a = LinuxApp("pad", "编辑器", "compute", cmd="mousepad", match="mousepad", wm_class="^mousepad$", kind="oss")
    env = dict(os.environ, XDG_CONFIG_HOME=str(tmp_path), XDG_CACHE_HOME=str(tmp_path), XDG_DATA_HOME=str(tmp_path))
    p = subprocess.Popen(["mousepad", "--disable-server", str(tmp_path / "doc.txt")], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 20
        wid = None
        while time.time() < deadline and not wid:
            hits = x11.windows_for([p.pid], class_rx="(?i)mousepad")
            wid = hits[0]["wid"] if hits else None
            time.sleep(0.3)
        assert wid, "mousepad window did not appear"
        assert x11.activate(wid, timeout=10)["ok"]
        time.sleep(0.5)

        class Dev:
            cat = {"pad": a}
        drv = W.WorkDriver(Dev())
        assert drv.spec(a) == {"kind": "edit", "oracle": "clipboard"}
        r = drv.work("pad", wid, 0.1, 7)
        assert r["status"] == "ok" and r["kind"] == "edit", r
        o1 = drv.oracle("pad", wid)
        assert o1["value"] and not o1["value"].startswith("0:"), o1
        clip = subprocess.run(["xclip", "-o", "-selection", "clipboard"], capture_output=True).stdout.decode()
        assert W.work_text("pad", 7).strip() in clip
        # nothing happened in between -> same content; more typing -> different content
        assert drv.compare(o1, drv.oracle("pad", wid), "pad") is True
        drv.work("pad", wid, 0.1, 8)
        assert drv.compare(o1, drv.oracle("pad", wid), "pad") is False
        # not the active window -> no keystrokes are sent anywhere
        root = x11.active_window()
        assert drv.work("pad", (root or 0) + 1, 0.05, 9)["status"] == "not-active"
    finally:
        p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
