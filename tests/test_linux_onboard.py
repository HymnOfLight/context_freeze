"""cf.linux.onboard: login knowledge per app, accounts / state files, main-window detection on
fake window lists, the non-interactive walk with a fake desktop + fake work driver, and - with
Xvfb - geometry of a real placeholder window."""
import json
import os
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import test_linux_x11  # noqa: E402
from test_linux_x11 import _xvfb_stack_available  # noqa: E402
from fake_linux_desktop import FakeWorkDriver  # noqa: E402

xvfb = test_linux_x11.xvfb

from cf.linux import apps as A  # noqa: E402
from cf.linux import onboard as OB  # noqa: E402
from cf.linux import x11  # noqa: E402
from cf.linux.apps import LinuxApp  # noqa: E402


# ------------------------------------------------------------------ knowledge table
def test_login_spec_covers_every_catalogue_app():
    for a in A.CATALOG.values():
        s = OB.login_spec(a)
        assert s["method"] in ("qr", "phone", "password", "account", "optional", "none"), a.id
        assert isinstance(s["main"], tuple) and len(s["main"]) == 2
        assert s["needs"] and s["prep"]
    assert OB.login_spec(A.CATALOG["wechat"])["method"] == "qr"
    assert OB.login_spec(A.CATALOG["wps"])["method"] == "optional"
    assert OB.login_spec(A.CATALOG["chrome"])["method"] == "none"
    # mobile builds share one entry; placeholders need nothing
    assert OB.login_spec(A.CATALOG["wechat_mobile"])["main"] == (0, 0)
    assert OB.login_spec(A.placeholder_for(A.CATALOG["wechat"]))["method"] == "none"
    # unknown desktop app -> generic entry; config overrides win field by field
    x = LinuxApp("x", "x", "push", "true", "x", "x")
    assert OB.login_spec(x)["main"] == OB.MAIN_WINDOW
    assert OB.login_spec(A.CATALOG["wechat"], {"wechat": {"main": [400, 300]}})["main"] == (400, 300)


def test_account_line_and_files(tmp_path):
    assert "accounts.example.json" in OB.account_line({}, "wechat")
    acc = {"phone": "+86 1", "nickname": "cf", "apps": {"qq": {"account": "123", "note": "scan"}}}
    assert OB.account_line(acc, "qq") == "账号 123 / 手机号 +86 1 / 昵称 cf / scan"
    assert OB.account_line(acc, "wps") == "手机号 +86 1 / 昵称 cf"
    assert OB.account_line({"apps": {}}, "wps") == "(accounts.json 里没填手机号)"
    assert OB.load_accounts(str(tmp_path / "missing.json")) == {}
    # the shipped template is valid json with the shared fields
    ex = OB.load_accounts(OB.ACCOUNTS_EXAMPLE)
    assert "phone" in ex and "wechat" in ex["apps"]
    # state round trip + readiness split
    p = str(tmp_path / "state.json")
    OB.save_state({"wechat": {"ready": True}, "qq": {"ready": False}}, p)
    st = OB.load_state(p)
    assert OB.not_ready(["wechat", "qq", "wps"], st) == (["wps"], ["qq"])
    assert OB.load_state(str(tmp_path / "nope.json")) == {}


# ------------------------------------------------------------------ detection on fake windows
class _Dev:
    def __init__(self, cat, pids=None, mobile=None):
        self.cat = cat
        self._pids = pids or {}
        self.mobile = mobile

    def pids(self, id_):
        return self._pids.get(id_, [])

    def available(self, id_):
        return True

    def window(self, id_, infos=None, pids=None):
        return {"wid": 7, "name": "android"}


def test_main_window_prefers_large_normal_windows():
    cat = A.CATALOG
    dev = _Dev(cat, {"wechat": [100]})
    infos = [
        {"wid": 1, "pid": 100, "wm_class": ["wechat", "wechat"], "name": "微信", "type": "", "state": []},     # QR dialog
        {"wid": 2, "pid": 100, "wm_class": ["wechat", "wechat"], "name": "微信", "type": "", "state": []},     # main
        {"wid": 3, "pid": 100, "wm_class": ["wechat", "wechat"], "name": "", "type": "_NET_WM_WINDOW_TYPE_DOCK",
         "state": []},                                                                                        # tray
    ]
    sizes = {1: (300, 400), 2: (1100, 760), 3: (2000, 2000)}
    spec = OB.login_spec(cat["wechat"])
    hit = OB.main_window(dev, "wechat", spec, infos, geom=lambda w: sizes.get(w))
    assert hit and hit["wid"] == 2 and hit["geometry"] == [1100, 760]
    # only the login dialog on screen -> not logged in
    small = [infos[0]]
    assert OB.main_window(dev, "wechat", spec, small, geom=lambda w: sizes.get(w)) is None
    # a window that vanished between listing and measuring is ignored
    assert OB.main_window(dev, "wechat", spec, [infos[1]], geom=lambda w: None) is None
    # windows found by WM_CLASS without a known pid count too
    assert OB.main_window(_Dev(cat), "wechat", spec, [infos[1]], geom=lambda w: sizes.get(w))["wid"] == 2


def test_main_window_mobile_needs_foreground_package():
    class MB:
        available = True

        def __init__(self, fg):
            self.fg = fg

        def foreground_pkg(self):
            return self.fg
    cat = A.CATALOG
    spec = OB.login_spec(cat["wechat_mobile"])
    assert OB.main_window(_Dev(cat, mobile=MB("com.tencent.mm")), "wechat_mobile", spec, []) == {"wid": 7, "name": "android"}
    assert OB.main_window(_Dev(cat, mobile=MB("com.other")), "wechat_mobile", spec, []) is None
    assert OB.main_window(_Dev(cat, mobile=None), "wechat_mobile", spec, []) is None


def test_parse_geometry():
    assert x11.parse_geometry("WINDOW=123\nX=10\nY=20\nWIDTH=800\nHEIGHT=600\nSCREEN=0\n") == (800, 600)
    assert x11.parse_geometry("") is None
    assert x11.parse_geometry("WIDTH=1") is None


def test_wait_for_non_interactive_detects_or_times_out():
    hits = iter([None, None, {"wid": 1}])
    got, how = OB.wait_for(lambda: next(hits, {"wid": 1}), timeout=5, prompt="", interactive=False, poll=0.01)
    assert got == {"wid": 1} and how == "detected"
    got, how = OB.wait_for(lambda: None, timeout=0.05, prompt="", interactive=False, poll=0.01)
    assert got is None and how == "timeout"
    assert OB.ask("x", interactive=False) is True


def test_wait_for_interactive_enter_and_skip(monkeypatch):
    def feed(text):
        r, w = os.pipe()
        os.write(w, text.encode()); os.close(w)
        monkeypatch.setattr(sys, "stdin", os.fdopen(r))
    feed("s\n")
    assert OB.wait_for(lambda: None, timeout=2, prompt="", interactive=True, poll=0.05, out=lambda s: None) == (None, "skipped")
    feed("\n")
    got, how = OB.wait_for(lambda: None, timeout=2, prompt="", interactive=True, poll=0.05, out=lambda s: None)
    assert (got, how) == (None, "confirmed")
    feed("\n")
    assert OB.ask("x", interactive=True, out=lambda s: None) is True
    feed("s\n")
    assert OB.ask("x", interactive=True, out=lambda s: None) is False


# ------------------------------------------------------------------ the walk (fake desktop)
class _WalkDev(_Dev):
    """Launch gives every app a window; `logged` says which ones show a main window."""

    def __init__(self, cat, logged, installed=None):
        super().__init__(cat)
        self.logged = set(logged)
        self.installed = installed
        self.launched = []
        self.caps = {"session_type": "x11"}

    def available(self, id_):
        return self.installed is None or id_ in self.installed

    def launch(self, id_):
        self.launched.append(id_)
        return {"status": "ok", "launch_state": "COLD", "wid": 0x1000 + len(self.launched)}


def test_onboard_walk_non_interactive(tmp_path, monkeypatch):
    cat = A.CATALOG
    dev = _WalkDev(cat, logged={"wps", "bilibili"}, installed={"wps", "bilibili", "wechat"})
    monkeypatch.setattr(OB, "main_window", lambda d, a, spec, infos=None, geom=None:
                        {"wid": 5, "geometry": [1000, 700], "name": a} if a in dev.logged else None)
    monkeypatch.setattr(x11, "activate", lambda wid, timeout=10: {"ok": True})
    work = FakeWorkDriver(dev)
    out = []
    state_path = str(tmp_path / "state.json")
    st = OB.onboard(dev, ["wps", "wechat", "bilibili", "qq"], work=work, interactive=False, timeout=0.05,
                    work_s=0, out=out.append, state_path=state_path)
    assert dev.launched == ["wps", "wechat", "bilibili"]                 # qq is not installed -> no launch
    assert st["wps"]["logged_in"] is True and st["wps"]["login_how"] == "detected"
    assert st["wps"]["ready"] is True and st["wps"]["rehearsal"]["kind"] == "edit"
    assert st["wps"]["rehearsal"]["work_status"] == "ok" and st["wps"]["rehearsal"]["stable"] is True
    assert st["wechat"]["logged_in"] is False and st["wechat"]["login_how"] == "timeout" and not st["wechat"]["ready"]
    assert st["qq"]["launch"] == "not-installed" and not st["qq"]["ready"]
    assert st["bilibili"]["ready"] is True
    assert ("wps", "work", 0) in work.calls and work.calls.count(("wps", "oracle", -1)) == 2
    assert json.load(open(state_path))["wps"]["ready"] is True
    text = "\n".join(out)
    assert "未安装" in text and "未登录 (timeout)" in text and "accounts" in text
    assert OB.not_ready(["wps", "wechat", "bilibili", "qq"], st) == ([], ["wechat", "qq"])
    assert "never onboarded" in OB.summary(["wps", "zwcad"], st)


def test_rehearse_reports_unstable_or_failed_work(monkeypatch):
    cat = A.CATALOG
    dev = _WalkDev(cat, logged={"wps"})
    monkeypatch.setattr(x11, "activate", lambda wid, timeout=10: {"ok": True})
    class Drifting(FakeWorkDriver):                     # every oracle read sees a different document
        def oracle(self, app_id, wid):
            o = super().oracle(app_id, wid)
            self.docs[app_id] += "x"
            return o
    r = OB.rehearse(Drifting(dev), dev, "wps", 5, 0, pause=0)
    assert r["work_status"] == "ok" and r["stable"] is False and r["ready"] is False

    class Dead(FakeWorkDriver):
        def work(self, app_id, wid, seconds, t):
            return {"kind": "edit", "status": "not-active", "elapsed_ms": 0}
    r = OB.rehearse(Dead(dev), dev, "wps", 5, 0, pause=0)
    assert r["work_status"] == "not-active" and r["ready"] is False
    # an app whose script is `none` with a `none` oracle is ready as soon as it is logged in
    cat2 = dict(cat)
    cat2["x"] = LinuxApp("x", "x", "compute", "true", "x", "x", work={"kind": "none", "oracle": "none"})
    dev2 = _WalkDev(cat2, logged={"x"})
    r = OB.rehearse(FakeWorkDriver(dev2), dev2, "x", None, 0, pause=0)
    assert r["ready"] is True


def test_check_table(tmp_path, monkeypatch):
    cat = A.CATALOG
    dev = _WalkDev(cat, logged={"wps"}, installed={"wps", "wechat"})
    dev._pids = {"wps": [1], "wechat": [2]}
    monkeypatch.setattr(OB, "main_window", lambda d, a, spec, infos=None, geom=None:
                        {"wid": 5, "geometry": [1000, 700]} if a in dev.logged else None)
    monkeypatch.setattr(OB, "load_state", lambda path=None: {"wps": {"ready": True}, "wechat": {"ready": True}})
    monkeypatch.setattr(OB, "seen_windows", lambda d, a, spec: "largest 640x400 < 700x500")
    out = []
    rc = OB.check(dev, ["wps", "wechat", "qq"], out=out.append)
    text = "\n".join(out)
    # onboarded, but its main window is gone now: the table says what it did find and why that is not enough
    assert rc == 1 and "wechat          True       True     largest 640x400 < 700x500" in text
    assert "no main window now" in text and "--discover" in text
    assert "wps             True       True     [1000, 700]" in text
    assert "qq              False" in text                                  # not installed: never counted as bad
    monkeypatch.setattr(OB, "main_window", lambda d, a, spec, infos=None, geom=None: {"wid": 5, "geometry": [9, 9]})
    assert OB.check(dev, ["wps", "wechat"], out=lambda s: None) == 0


def test_check_explains_each_failure_mode(monkeypatch):
    cat = A.CATALOG
    ap = cat["wechat"]
    why = OB._why_not_ready
    assert why({}, [1], None, ap) == "never onboarded"
    assert why({"launch": "TIMEOUT"}, [], None, ap) == "not running (TIMEOUT)"          # crashed: see app_logs
    assert why({"launch": "COLD", "login_how": "timeout"}, [1], {"wid": 1}, ap) == "login timeout"
    assert why({"launch": "COLD", "logged_in": True, "login_how": "confirmed"}, [1], None, ap).startswith("no main window now")
    rec = {"launch": "COLD", "logged_in": True, "login_how": "detected", "prep": True,
           "rehearsal": {"oracle": "clipboard", "work_status": "not-active", "stable": None}}
    assert why(rec, [1], {"wid": 1}, ap) == "work not-active"
    rec["rehearsal"] = {"oracle": "clipboard", "work_status": "ok", "stable": None, "oracle_error": "xclip: [Errno 2]"}
    assert why(rec, [1], {"wid": 1}, ap) == "oracle clipboard: xclip: [Errno 2]"
    rec["rehearsal"] = {"oracle": "clipboard", "work_status": "ok", "stable": None}
    assert why(rec, [1], {"wid": 1}, ap) == "oracle clipboard read nothing"
    rec["rehearsal"] = {"oracle": "clipboard", "work_status": "ok", "stable": False}
    assert why(rec, [1], {"wid": 1}, ap) == "oracle clipboard unstable"
    # the summary table carries the oracle error and a non-automatic login too
    st = {"wechat": {"logged_in": True, "login_how": "confirmed", "ready": False, "method": "qr", "at": "2026-10-27T09:58",
                     "rehearsal": {"kind": "draft", "oracle": "clipboard", "work_status": "not-active", "stable": None,
                                   "oracle_error": "not-active"}}}
    row = OB.summary(["wechat"], st)
    assert "err=not-active" in row and "login=confirmed" in row


def test_seen_windows_describes_what_was_found(monkeypatch):
    cat = A.CATALOG
    dev = _Dev(cat, {"wechat": [100], "wps": [200], "qq": [300]})
    infos = {
        100: [{"wid": 1, "pid": 100, "wm_class": ["wechat", "wechat"], "name": "微信", "type": "", "state": []}],
        200: [{"wid": 2, "pid": 200, "wm_class": ["wps", "wps"], "name": "", "type": "_NET_WM_WINDOW_TYPE_DOCK", "state": []}],
        300: [],
    }
    monkeypatch.setattr(OB.x11, "windows_for", lambda pids, c=None, n=None, infos_=None: infos.get(next(iter(pids), 0), []))
    monkeypatch.setattr(OB.x11, "geometry", lambda wid: {1: (640, 400), 2: (50, 50)}.get(wid))
    assert OB.seen_windows(dev, "wechat", OB.login_spec(cat["wechat"])) == "largest 640x400 < 700x500"
    assert OB.seen_windows(dev, "wps", OB.login_spec(cat["wps"])) == "1 non-normal (dock)"
    assert OB.seen_windows(dev, "qq", OB.login_spec(cat["qq"])) == "no window matched"
    assert OB.seen_windows(_Dev(cat), "qq", OB.login_spec(cat["qq"])) == "-"            # not running at all


def test_onboard_repicks_the_window_after_a_manual_confirmation(tmp_path, monkeypatch):
    """The launch sees the QR dialog; after Enter the main window is a different wid - rehearse that one."""
    cat = A.CATALOG
    dev = _WalkDev(cat, logged=set(), installed={"wechat"})
    dev.window = lambda id_, infos=None, pids=None: {"wid": 0x2222, "name": "微信"}       # what exists now
    monkeypatch.setattr(OB, "main_window", lambda d, a, spec, infos=None, geom=None: None)
    monkeypatch.setattr(OB, "wait_for", lambda cond, timeout, prompt, interactive, out=print: (None, "confirmed"))
    monkeypatch.setattr(OB, "ask", lambda prompt, interactive, out=print: True)
    seen = {}
    class Rec(FakeWorkDriver):
        def work(self, app_id, wid, seconds, t):
            seen["wid"] = wid
            return super().work(app_id, wid, seconds, t)
    monkeypatch.setattr(x11, "activate", lambda wid, timeout=10: {"ok": True})
    st = OB.onboard(dev, ["wechat"], work=Rec(dev), interactive=True, timeout=0, work_s=0,
                    out=lambda s: None, state_path=str(tmp_path / "s.json"))
    assert seen["wid"] == 0x2222 and st["wechat"]["wid"] == 0x2222 and st["wechat"]["logged_in"] is True


# ------------------------------------------------------------------ Xvfb: real geometry
@pytest.mark.skipif(not _xvfb_stack_available(), reason="needs Xvfb + a WM + xdotool/xprop + python3-xlib")
def test_geometry_of_a_real_window(xvfb, tmp_path):
    env = dict(os.environ, PYTHONPATH=os.getcwd())
    p = subprocess.Popen([sys.executable, "-m", "cf.linux.placeholder_app", "--app", "sim_g", "--title", "sim_g",
                          "--cpu", "0.05", "--mem", "8", "--state-file", str(tmp_path / "g.json")],
                         env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        hit = None
        deadline = time.time() + 15
        while time.time() < deadline and not hit:
            time.sleep(0.3)
            hit = x11.windows_for([p.pid], class_rx="^cf-sim_g$")
        assert hit, "placeholder window did not appear"
        g = x11.geometry(hit[0]["wid"])
        assert g and g[0] > 50 and g[1] > 50
        assert x11.geometry(0x7FFFFFF) is None

        class D:
            cat = {"sim_g": LinuxApp("sim_g", "g", "compute", "true", "x", r"^cf-sim_g$", kind="placeholder")}
            mobile = None

            def pids(self, id_):
                return [p.pid]
        spec = {"main": (10, 10)}
        w = OB.main_window(D(), "sim_g", spec)
        assert w and w["wid"] == hit[0]["wid"] and w["geometry"] == list(g)
        assert OB.main_window(D(), "sim_g", {"main": (g[0] + 1, g[1])}) is None
    finally:
        p.terminate()
        p.wait(timeout=5)
