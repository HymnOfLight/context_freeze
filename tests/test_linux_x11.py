"""X11 helpers: xprop parsing (pure) and, when Xvfb + a window manager + xdotool are available,
a real round trip: map a placeholder window, find it, activate it, _NET_WM_PING it."""
import os
import shutil
import subprocess
import sys
import time

import pytest

from cf.linux import x11

XPROP = """\
_NET_WM_PID(CARDINAL) = 4242
WM_CLASS(STRING) = "wechat", "WeChat"
_NET_WM_NAME(UTF8_STRING) = "微信"
WM_NAME(STRING) = "WeChat"
WM_PROTOCOLS(ATOM): protocols  WM_DELETE_WINDOW, _NET_WM_PING, _NET_WM_SYNC_REQUEST
_NET_WM_STATE(ATOM) = _NET_WM_STATE_HIDDEN, _NET_WM_STATE_SKIP_TASKBAR
_NET_WM_WINDOW_TYPE(ATOM) = _NET_WM_WINDOW_TYPE_NORMAL
_NET_WM_DESKTOP(CARDINAL) = 0
"""


def test_parse_xprop():
    info = x11.parse_xprop(XPROP)
    assert info["pid"] == 4242
    assert info["wm_class"] == ["wechat", "WeChat"]
    assert info["name"] == "微信"                      # _NET_WM_NAME wins over WM_NAME
    assert info["protocols"] == ["WM_DELETE_WINDOW", "_NET_WM_PING", "_NET_WM_SYNC_REQUEST"]
    assert info["state"] == ["_NET_WM_STATE_HIDDEN", "_NET_WM_STATE_SKIP_TASKBAR"]
    assert info["type"] == "_NET_WM_WINDOW_TYPE_NORMAL" and info["desktop"] == 0


def test_parse_xprop_missing_props():
    info = x11.parse_xprop("_NET_WM_PID:  not found.\nWM_CLASS(STRING) = \"Navigator\", \"firefox\"\n")
    assert info["pid"] is None and info["wm_class"] == ["Navigator", "firefox"] and info["name"] == ""


def test_parse_window_ids():
    assert x11.parse_window_ids("_NET_CLIENT_LIST(WINDOW): window id # 0x1400003, 0x1600004") == [0x1400003, 0x1600004]
    assert x11.parse_window_ids("20971523\n23068676\n") == [20971523, 23068676]
    assert x11.parse_window_ids("_NET_CLIENT_LIST(WINDOW): window id #") == []


def test_windows_for_prefers_main_window_over_tray_popup():
    infos = [
        {"wid": 1, "pid": 10, "wm_class": ["wechat", "WeChat"], "name": "", "state": ["_NET_WM_STATE_SKIP_TASKBAR"],
         "type": "_NET_WM_WINDOW_TYPE_DOCK"},
        {"wid": 2, "pid": 10, "wm_class": ["wechat", "WeChat"], "name": "微信", "state": [], "type": ""},
        {"wid": 3, "pid": 11, "wm_class": ["qq", "QQ"], "name": "QQ", "state": [], "type": ""},
        {"wid": 4, "pid": None, "wm_class": ["wps", "Wps"], "name": "文档 - WPS", "state": [], "type": ""},
    ]
    assert [w["wid"] for w in x11.windows_for([10], infos=infos)] == [2, 1]
    assert [w["wid"] for w in x11.windows_for([], class_rx="^wps$", infos=infos)] == [4]
    assert [w["wid"] for w in x11.windows_for([], name_rx="WPS", infos=infos)] == [4]
    assert x11.windows_for([99], infos=infos) == []


# ------------------------------------------------------------------ integration (Xvfb)
def _xvfb_stack_available() -> bool:
    if any(shutil.which(t) is None for t in ("Xvfb", "xdotool", "xprop")):
        return False
    if not any(shutil.which(wm) for wm in ("xfwm4", "openbox", "metacity", "mutter")):
        return False
    try:
        import Xlib  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.fixture
def xvfb():
    disp = ":%d" % (90 + os.getpid() % 50)
    xvfb = subprocess.Popen(["Xvfb", disp, "-screen", "0", "1024x768x24"], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    env = dict(os.environ, DISPLAY=disp)
    for _ in range(50):
        time.sleep(0.1)
        if subprocess.run(["xdpyinfo"], env=env, capture_output=True).returncode == 0 or \
           subprocess.run(["xprop", "-root"], env=env, capture_output=True).returncode == 0:
            break
    wm_bin = next(w for w in ("xfwm4", "openbox", "metacity", "mutter") if shutil.which(w))
    wm_args = [wm_bin] + (["--sm-client-disable"] if wm_bin == "xfwm4" else [])
    wm = subprocess.Popen(wm_args, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.0)
    old = os.environ.get("DISPLAY")
    os.environ["DISPLAY"] = disp
    try:
        yield disp
    finally:
        if old is None:
            os.environ.pop("DISPLAY", None)
        else:
            os.environ["DISPLAY"] = old
        for p in (wm, xvfb):
            p.terminate()
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()


@pytest.mark.skipif(not _xvfb_stack_available(), reason="needs Xvfb + a WM + xdotool/xprop + python3-xlib")
def test_placeholder_window_discover_activate_ping(xvfb, tmp_path):
    env = dict(os.environ, PYTHONPATH=os.getcwd())
    procs = []
    for app in ("sim_a", "sim_b"):
        procs.append(subprocess.Popen([sys.executable, "-m", "cf.linux.placeholder_app", "--app", app, "--title", app,
                                       "--cpu", "0.05", "--mem", "8", "--state-file", str(tmp_path / f"{app}.json")],
                                      env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    try:
        wins = {}
        deadline = time.time() + 15
        while time.time() < deadline and len(wins) < 2:
            time.sleep(0.3)
            for app, p in zip(("sim_a", "sim_b"), procs):
                hit = x11.windows_for([p.pid], class_rx=f"^cf-{app}$")
                if hit:
                    wins[app] = hit[0]
        assert set(wins) == {"sim_a", "sim_b"}, "placeholder windows did not appear"
        assert wins["sim_a"]["pid"] == procs[0].pid
        assert "_NET_WM_PING" in wins["sim_a"]["protocols"]

        pinger = x11.Pinger()
        assert pinger.available, pinger.err
        for app in ("sim_a", "sim_b", "sim_a"):
            wid = wins[app]["wid"]
            act = x11.activate(wid, timeout=10)
            assert act["ok"], act
            assert act["activate_ms"] is not None and act["activate_ms"] >= 0
            assert x11.active_window() == wid
            assert pinger.supports(wid)
            ms = pinger.ping(wid, timeout=10)
            assert ms is not None and 0 <= ms < 5000
        pinger.close()
        # the window the app does not own is ignored
        assert x11.windows_for([procs[0].pid], class_rx="^cf-sim_b$") and \
            all(w["pid"] == procs[1].pid for w in x11.windows_for([], class_rx="^cf-sim_b$"))
        assert x11.wm_name() != ""
        # the placeholder keeps its state file up to date (1 s cadence) and the counter advances
        import json
        st = {}
        for _ in range(60):
            try:
                st = json.load(open(tmp_path / "sim_a.json"))
            except (OSError, ValueError):
                st = {}
            if st.get("counter", 0) > 0:
                break
            time.sleep(0.1)
        assert st.get("counter", 0) > 0 and st["pid"] == procs[0].pid
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
