"""focusd decision logic on a fake cgroupfs: focus -> fg, others -> shrunk after the grace period,
budgeted residents -> bg, never_throttle untouched, release on stop."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from cf.linux.cgroup import PERIOD_US, CgroupTree  # noqa: E402
from cf.linux.focusd import FocusDaemon  # noqa: E402

MB = 2 ** 20
CFG = {"throttle": {"cpu_cores": 1, "mem_frac": 0.5, "mem_min_mb": 32, "proactive_reclaim": False},
       "never_throttle": ["music"]}


def fake_tree(tmp_path, apps):
    base = tmp_path / "cf"
    base.mkdir()
    (base / "cgroup.controllers").write_text("cpuset cpu io memory\n")
    (base / "cgroup.procs").write_text("")
    (tmp_path / "cpuset.cpus.effective").write_text("0-3\n")
    tree = CgroupTree(str(tmp_path), "cf")
    tree.ensure("host")
    for i, (a, mb) in enumerate(apps.items()):
        tree.ensure(a)
        (base / a / "memory.current").write_text(str(mb * MB))
        (base / a / "cgroup.procs").write_text(f"{1000 + i}\n")
        (base / a / "cgroup.events").write_text("populated 1\nfrozen 0\n")
    return tree


def knob(tree, leaf, f):
    return tree.read(leaf, f)


def test_static_focus_limits_everything_else_after_delay(tmp_path):
    tree = fake_tree(tmp_path, {"wechat": 600, "wps": 900, "music": 200})
    d = FocusDaemon(CFG, delay_s=0.0, tree=tree, log=lambda *a: None)
    assert sorted(d.leaves()) == ["music", "wechat", "wps"]
    d.on_focus("wps")
    d.flush_pending()
    tier = lambda l: d.tiers.get(l, "fg")   # noqa: E731
    assert (tier("wps"), tier("wechat"), tier("music")) == ("fg", "shrunk", "fg")
    assert knob(tree, "wechat", "cpu.max") == f"{PERIOD_US} {PERIOD_US}"
    assert knob(tree, "wechat", "memory.high") == str(300 * MB)        # 50 % of its 600 MB
    assert knob(tree, "wechat", "memory.max") == "max"
    assert knob(tree, "music", "cpu.max") == ""                          # never touched
    assert d.m_fg["wps"] == 900 * MB

    d.on_focus("wechat")                       # alt-tab back
    assert knob(tree, "wechat", "cpu.max") == f"max {PERIOD_US}" and knob(tree, "wechat", "memory.high") == "max"
    d.flush_pending()
    assert tier("wps") == "shrunk" and knob(tree, "wps", "memory.high") == str(450 * MB)

    d.on_focus(None)                           # focus on an unmanaged window: everything is background
    d.flush_pending()
    assert tier("wechat") == "shrunk" and tier("wps") == "shrunk" and tier("music") == "fg"
    assert d.release_all() == 3
    assert knob(tree, "wps", "cpu.max") == f"max {PERIOD_US}" and knob(tree, "wps", "memory.high") == "max"


def test_grace_period_cancels_on_quick_return(tmp_path, monkeypatch):
    tree = fake_tree(tmp_path, {"wechat": 600, "wps": 900})
    d = FocusDaemon(CFG, delay_s=100.0, tree=tree, log=lambda *a: None)
    d.on_focus("wps")
    d.flush_pending()                          # wechat is pending, not due
    assert d.tiers.get("wechat", "fg") == "fg" and "wechat" in d.pending
    d.on_focus("wechat")                       # came back in time: nothing was ever written
    assert "wechat" not in d.pending and knob(tree, "wechat", "cpu.max") == ""


def test_budgeted_policy_keeps_residents_cpu_only(tmp_path):
    tree = fake_tree(tmp_path, {"wechat": 600, "wps": 900, "cad": 1200, "qq": 300})
    d = FocusDaemon(CFG, policy="lru", eta=0.5, delay_s=0.0, tree=tree, log=lambda *a: None)
    for leaf in ("wechat", "wps", "cad", "qq", "wechat"):
        d.on_focus(leaf)
        d.flush_pending()
    assert d.tiers.get("wechat", "fg") == "fg"
    others = {l: d.tiers.get(l, "fg") for l in ("wps", "cad", "qq")}
    assert set(others.values()) <= {"bg", "shrunk"} and "bg" in others.values()
    for l, t in others.items():
        assert knob(tree, l, "cpu.max") == f"{PERIOD_US} {PERIOD_US}"
        if t == "bg":
            assert knob(tree, l, "memory.high") == "max"
        else:
            assert knob(tree, l, "memory.high") != "max"


def test_dry_run_touches_nothing(tmp_path):
    tree = fake_tree(tmp_path, {"wechat": 600, "wps": 900})
    d = FocusDaemon(CFG, delay_s=0.0, tree=tree, dry_run=True, log=lambda *a: None, jsonl=str(tmp_path / "f.jsonl"))
    d.on_focus("wps")
    d.flush_pending()
    assert d.tiers["wechat"] == "shrunk" and knob(tree, "wechat", "cpu.max") == ""
    d.release_all()
    d.jsonl.close()
    lines = (tmp_path / "f.jsonl").read_text().splitlines()
    assert len(lines) == 2 and '"type": "focus"' in lines[0] and '"type": "limit"' in lines[1]
