"""Mobile (Android-in-Waydroid) builds on the Ubuntu back-end: parsers, and LinuxDesktop driving a
mobile app through a fake runtime (launch = activate the Android window + `am start -W`, host
processes adopted into the cgroup, notifications via dumpsys, runner preflight / substitution)."""
from __future__ import annotations

import pytest

from cf.linux import apps as A
from cf.linux import device as D
from cf.linux import mobile as M
from cf.linux import x11
from cf.linux.procs import ProcInfo

STATUS = """\
Session:	RUNNING
Container:	RUNNING
Vendor type:	MAINLINE
IP address:	192.168.240.112
Session user:	ubuntu(1000)
Wayland display:	waydroid
"""

DUMPSYS = """\
  NotificationRecord(0x4a1b2c3: pkg=com.tencent.mm user=UserHandle{0} id=12 tag=null importance=3 key=0|com.tencent.mm|12|null|10089: Notification(channel=message ...))
      uid=10089 opPkg=com.tencent.mm
  NotificationRecord(0x4a1b2c4: pkg=com.tencent.mm user=UserHandle{0} id=13 tag=null importance=3 key=0|com.tencent.mm|13|null|10089: Notification(...))
  NotificationRecord(0x5f: pkg=com.alibaba.android.rimet user=UserHandle{0} id=1 tag=ding importance=4 key=0|com.alibaba.android.rimet|1|ding|10091: Notification(...))
  NotificationRecord(0x60: pkg=android user=UserHandle{0} id=27 tag=null importance=2 key=0|android|27|null|1000: Notification(...))
"""

ACTIVITIES = """\
  ResumedActivity: ActivityRecord{1234 u0 com.lemon.lv/com.vega.main.MainActivity t12}
    topResumedActivity=ActivityRecord{1234 u0 com.lemon.lv/com.vega.main.MainActivity t12}
"""


def test_parse_waydroid_status():
    st = M.parse_waydroid_status(STATUS)
    assert st["session"] == "RUNNING" and st["ip_address"] == "192.168.240.112"
    assert st["wayland_display"] == "waydroid"
    assert M.parse_waydroid_status("") == {}


def test_parse_dumpsys_notifications_groups_keys_per_package():
    n = M.parse_dumpsys_notifications(DUMPSYS)
    assert len(n["com.tencent.mm"]) == 2 and len(n["com.alibaba.android.rimet"]) == 1
    assert "android" in n


def test_parse_resumed_pkg():
    assert M.parse_resumed_pkg(ACTIVITIES) == "com.lemon.lv"
    assert M.parse_resumed_pkg("nothing") is None


def test_notification_snapshot_is_cumulative_and_keyed_mobile_pkg(monkeypatch):
    rt = M.MobileRuntime({"backend": "waydroid"})
    rt.caps = {"available": True}
    texts = iter([DUMPSYS, DUMPSYS, DUMPSYS.replace("id=13", "id=14").replace("|13|", "|14|")])
    monkeypatch.setattr(rt, "shell", lambda cmd, timeout=60: next(texts))
    s1 = rt.notification_snapshot()
    s2 = rt.notification_snapshot()
    s3 = rt.notification_snapshot()
    assert s1["mobile:com.tencent.mm"] == 2 and s2 == s1       # same keys again -> no growth
    assert s3["mobile:com.tencent.mm"] == 3                     # a new key -> +1 (dismissed ones stay counted)


# --------------------------------------------------------------- fake runtime + device round trip
class FakeTree:
    base = "/fake/cf"

    def __init__(self):
        self.members: dict[str, list[int]] = {}
        self.attach_ok = True

    def path(self, name=""):
        return f"{self.base}/{name}" if name else self.base

    def ensure(self, name):
        self.members.setdefault(name, [])

    def exists(self, name=""):
        return name == "" or name in self.members

    def procs(self, name):
        return list(self.members.get(name, []))

    def attach(self, name, pids):
        if not self.attach_ok:
            return [], {p: "EACCES" for p in pids}
        self.ensure(name)
        for p in pids:
            if p not in self.members[name]:
                self.members[name].append(p)
        return list(pids), {}

    def stats(self, name):
        return {"memory_current": 0, "cpu": {}, "events": {}}

    def leaves(self):
        return sorted(self.members)

    def clear_limits(self, name):
        pass


class FakeRuntime:
    """Android as seen through cf.linux.mobile.MobileRuntime, without Waydroid."""
    wm_class = A.MOBILE_WM_CLASS

    def __init__(self, installed, host_pids):
        self._installed = set(installed)
        self._host = dict(host_pids)            # pkg -> pids once started
        self.started: list[str] = []
        self.fg = None
        self.caps = {"available": True, "backend": "waydroid", "per_app_cgroups": True, "adb_serial": "x:5555"}
        self.notifs: dict[str, int] = {}
        self.launch_state = "COLD"

    available = property(lambda self: True)

    def probe(self):
        return self.caps

    def ensure_session(self):
        return True

    def installed(self, pkg):
        return pkg in self._installed

    def host_pids(self, pkg):
        return list(self._host.get(pkg, [])) if pkg in self.started else []

    def foreground_pkg(self):
        return self.fg

    def launch(self, pkg, timeout=120):
        first = pkg not in self.started
        if first:
            self.started.append(pkg)
        self.fg = pkg
        state = "COLD" if first else "HOT"
        return {"status": "ok", "launch_state": state, "total_time_ms": 480 if first else 90,
                "this_time_ms": None, "wait_time_ms": None, "component": f"{pkg}/.Main", "am_elapsed_ms": 500}

    def notification_snapshot(self):
        return {f"mobile:{k}": v for k, v in self.notifs.items()}

    def describe(self):
        return "fake android"


WESTON = {"wid": 0x400001, "pid": 999, "wm_class": ["weston", "weston"], "name": "Waydroid", "type": "", "state": []}


@pytest.fixture
def mobile_desktop(monkeypatch):
    rt = FakeRuntime(installed={"com.lemon.lv", "com.tencent.mm"},
                     host_pids={"com.lemon.lv": [5001, 5002], "com.tencent.mm": [6001]})
    tree = FakeTree()
    cat = A.with_overrides({})
    dev = D.LinuxDesktop(cat=cat, tree=tree, notifications=False, mobile=rt)
    active = {"wid": None}
    monkeypatch.setattr(x11, "windows", lambda with_cmdline=False: [WESTON])
    monkeypatch.setattr(x11, "have_display", lambda: True)
    monkeypatch.setattr(x11, "active_window", lambda: active["wid"])

    def activate(wid, timeout=10.0):
        active["wid"] = wid
        return {"ok": True, "activate_ms": 25}
    monkeypatch.setattr(x11, "activate", activate)
    monkeypatch.setattr(D.PR, "proc_info", lambda pid: ProcInfo(pid=pid, pss_kb=1000, age_s=10.0))
    monkeypatch.setattr(D, "time", type("T", (), {"monotonic": staticmethod(__import__("time").monotonic),
                                                   "time": staticmethod(__import__("time").time),
                                                   "sleep": staticmethod(lambda s: None)}))
    return dev, rt, tree, active


def test_mobile_launch_activates_window_am_starts_and_adopts_processes(mobile_desktop):
    dev, rt, tree, active = mobile_desktop
    assert dev.available("jianying_mobile") and not dev.available("wps_mobile")
    r = dev.launch("jianying_mobile")
    assert r["status"] == "ok" and r["mobile"] is True
    assert r["launch_state"] == "COLD" and r["total_time_ms"] == 25 + 480 and r["ping_ms"] is None
    assert r["wid"] == WESTON["wid"] and active["wid"] == WESTON["wid"]
    assert rt.started == ["com.lemon.lv"]
    assert sorted(tree.procs("jianying_mobile")) == [5001, 5002] and sorted(r["adopted"]) == [5001, 5002]
    assert r["pid"] in (5001, 5002)
    # second switch: Android says HOT; the window is already active but another app was in front
    r2 = dev.launch("wechat_mobile")
    assert r2["launch_state"] == "COLD" and sorted(tree.procs("wechat_mobile")) == [6001]
    r3 = dev.launch("jianying_mobile")
    assert r3["launch_state"] == "HOT" and r3["total_time_ms"] == 25 + 90
    # same app again while in front -> FRONT, no latency sample
    r4 = dev.launch("jianying_mobile")
    assert r4["launch_state"] == "FRONT" and r4["total_time_ms"] is None


def test_mobile_sample_readopts_new_processes_and_counts_dumpsys_notifications(mobile_desktop):
    dev, rt, tree, _ = mobile_desktop
    dev.launch("wechat_mobile")
    rt._host["com.tencent.mm"].append(6002)                    # Android forked :push
    s1 = dev.sample(["wechat_mobile"])
    smp = s1["apps"]["wechat_mobile"]
    assert sorted(p.pid for p in smp.procs) == [6001, 6002] and smp.stray_pids == []
    assert sorted(tree.procs("wechat_mobile")) == [6001, 6002]
    assert smp.window == WESTON["wid"] and smp.to_dict()["pss_kb"] == 2000
    rt.notifs["com.tencent.mm"] = 3
    s2 = dev.sample(["wechat_mobile"])
    assert dev.notifications_for("wechat_mobile", s1["notifications"], s2["notifications"]) == 3
    assert dev.notifications_for("wechat_mobile", s2["notifications"], s2["notifications"]) == 0
    # when the processes cannot be moved they are still measured but reported as strays
    tree.attach_ok = False
    rt._host["com.tencent.mm"].append(6003)
    smp3 = dev.sample(["wechat_mobile"])["apps"]["wechat_mobile"]
    assert 6003 in [p.pid for p in smp3.procs] and smp3.stray_pids == [6003]
    assert D.LinuxDesktop.state_check(smp.to_dict(), smp3.to_dict()) == {}


def test_mobile_launch_without_runtime_reports_it(monkeypatch):
    dev = D.LinuxDesktop(cat=A.with_overrides({}), tree=FakeTree(), notifications=False, mobile=None)
    r = dev.launch("jianying_mobile")
    assert r["status"] == "no-android-runtime" and r["launch_state"] == "TIMEOUT"
    assert not dev.available("jianying_mobile")


def test_runner_prepare_substitutes_mobile_build_and_preflights_runtime(monkeypatch, tmp_path):
    from cf.linux.runner import ThrottleExperiment
    from cf.runner import PreflightError
    from tests.fake_linux_desktop import FakeLinuxDesktop, OVERRIDES

    base = {"name": "m", "out_dir": str(tmp_path), "apps": ["fake_office", "fake_dl", "jianying"],
            "app_overrides": OVERRIDES, "policy": "static", "trace": {"kind": "zipf", "T": 2},
            "strict_preflight": True, "notifications": False}

    class Dev(FakeLinuxDesktop):
        """fake desktop with an Android runtime that has 剪映 installed (desktop builds: the fakes only)"""

        def __init__(self, apps, rt):
            super().__init__(apps)
            self.mobile = rt

        def probe(self):
            c = super().probe()
            c["mobile"] = dict(self.mobile.caps)
            return c

    rt = FakeRuntime(installed={"com.lemon.lv"}, host_pids={"com.lemon.lv": [1]})
    exp = ThrottleExperiment(base, Dev(["fake_office", "fake_dl"], rt))
    exp.open_log()
    exp.prepare()
    assert exp.apps == ["fake_office", "fake_dl", "jianying_mobile"]
    assert exp.substitutions == [("jianying", "jianying_mobile")]
    assert exp.kinds["jianying_mobile"] == "mobile" and exp.kinds["fake_dl"] == "desktop"
    assert any("sudo_adopt" in w for w in exp.log.warnings)       # Android pids need root to be moved
    exp.close()

    # no Android runtime reachable -> blocking preflight problem (strict), warning otherwise
    rt_down = FakeRuntime(installed={"com.lemon.lv"}, host_pids={})
    rt_down.caps = {"available": False, "backend": "waydroid", "adb_error": "adb not found"}
    exp2 = ThrottleExperiment({**base, "name": "m2"}, Dev(["fake_office", "fake_dl"], rt_down))
    exp2.open_log()
    with pytest.raises(PreflightError):
        exp2.prepare()
    exp2.close()
    exp3 = ThrottleExperiment({**base, "name": "m3", "strict_preflight": False}, Dev(["fake_office", "fake_dl"], rt_down))
    exp3.open_log()
    exp3.prepare()
    assert "jianying_mobile" in exp3.apps and any("no Android runtime" in w for w in exp3.log.warnings)
    exp3.close()
    # the oss look-alike is used only when substitute_kinds says so
    exp4 = ThrottleExperiment({**base, "name": "m4", "substitute_kinds": ["oss"]}, Dev(["fake_office", "fake_dl"], rt))
    exp4.open_log()
    exp4.prepare()                         # kdenlive is not installed and the mobile build is not allowed -> dropped
    assert exp4.apps == ["fake_office", "fake_dl"] and exp4.substitutions == []
    assert any("jianying" in w and "dropped" in w for w in exp4.log.warnings)
    exp4.close()
