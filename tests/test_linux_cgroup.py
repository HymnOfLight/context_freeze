"""cgroup v2 tier logic: the knob values each tier writes (pure), the tree on a fake cgroupfs
(a plain directory), and - when /sys/fs/cgroup/cf is delegated to us - the real thing."""
import os

import pytest

from cf.linux.cgroup import (PERIOD_US, CgroupTree, ThrottleProfile, apply_tier, parse_cpu_list, parse_kv,
                             pid_cgroup_path, self_cgroup_path, tier_limits)

MB = 2 ** 20


def test_profile_from_cfg_ignores_unknown_keys_and_describes():
    p = ThrottleProfile.from_cfg({"cpu_cores": 2, "mem_frac": 0.25, "bogus": 1, "swap_max_mb": 512})
    assert p.cpu_cores == 2 and p.mem_frac == 0.25 and p.swap_max_mb == 512
    assert p.cpu_mode == "quota" and p.mem_min_mb == 192
    d = p.describe()
    assert "2 core quota" in d and "25%" in d and "swap.max=512 MB" in d
    assert ThrottleProfile.from_cfg(None) == ThrottleProfile()
    assert ThrottleProfile.from_cfg(p.to_dict()) == p


def test_memory_high_has_a_floor():
    p = ThrottleProfile(mem_frac=0.5, mem_min_mb=192)
    assert p.mem_high_bytes(1000 * MB) == 500 * MB
    assert p.mem_high_bytes(100 * MB) == 192 * MB         # never below the floor
    assert ThrottleProfile(mem_frac=1.0, mem_min_mb=0).mem_high_bytes(300 * MB) == 300 * MB


def test_tier_limits_fg_clears_everything(tmp_path):
    tree = CgroupTree(str(tmp_path), "cf")
    lim = tier_limits(tree, ThrottleProfile(), "fg", 800 * MB)
    assert lim["cpu_max"] == f"max {PERIOD_US}" and lim["cpuset"] == "" and lim["cpu_weight"] == 100
    assert lim["memory_high"] == "max" and lim["memory_max"] == "max" and lim["swap_max"] == "max"


def test_tier_limits_bg_is_cpu_only(tmp_path):
    tree = CgroupTree(str(tmp_path), "cf")
    lim = tier_limits(tree, ThrottleProfile(cpu_cores=1.0, cpu_weight=20), "bg", 800 * MB)
    assert lim["cpu_max"] == f"{PERIOD_US} {PERIOD_US}"       # exactly one core
    assert lim["cpu_weight"] == 20
    assert lim["memory_high"] == "max" and lim["memory_max"] == "max" and lim["swap_max"] == "max"
    half = tier_limits(tree, ThrottleProfile(cpu_cores=0.5), "bg")
    assert half["cpu_max"] == f"{PERIOD_US // 2} {PERIOD_US}"


def test_tier_limits_shrunk_adds_memory_high_but_no_hard_limit(tmp_path):
    tree = CgroupTree(str(tmp_path), "cf")
    lim = tier_limits(tree, ThrottleProfile(mem_frac=0.5), "shrunk", 800 * MB)
    assert lim["memory_high"] == str(400 * MB)
    assert lim["memory_max"] == "max", "state must never be OOM-killed by default"
    assert lim["swap_max"] == "max"
    hard = tier_limits(tree, ThrottleProfile(mem_frac=0.5, mem_hard=True, mem_hard_factor=1.5, swap_max_mb=256),
                       "shrunk", 800 * MB)
    assert hard["memory_max"] == str(600 * MB) and hard["swap_max"] == str(256 * MB)


def test_tier_limits_cpuset_modes(tmp_path):
    root = tmp_path
    (root / "cpuset.cpus.effective").write_text("0-3\n")
    tree = CgroupTree(str(root), "cf")
    assert tree.effective_cpus() == [0, 1, 2, 3]
    only_set = tier_limits(tree, ThrottleProfile(cpu_mode="cpuset", cpuset_cpus="last"), "bg")
    assert only_set["cpuset"] == "3" and only_set["cpu_max"] == f"max {PERIOD_US}"
    both = tier_limits(tree, ThrottleProfile(cpu_mode="both", cpuset_cpus="2-3", cpu_cores=1), "bg")
    assert both["cpuset"] == "2-3" and both["cpu_max"] == f"{PERIOD_US} {PERIOD_US}"
    none = tier_limits(tree, ThrottleProfile(cpu_mode="none"), "bg")
    assert none["cpu_max"] == f"max {PERIOD_US}" and none["cpuset"] == ""
    with pytest.raises(ValueError):
        tier_limits(tree, ThrottleProfile(), "frozen")


def test_parsers():
    assert parse_cpu_list("0-3,6,8-9") == [0, 1, 2, 3, 6, 8, 9]
    assert parse_cpu_list("") == []
    assert parse_kv("usage_usec 12\nnr_throttled 3\nthrottled_usec 400\n") == {
        "usage_usec": 12, "nr_throttled": 3, "throttled_usec": 400}
    assert self_cgroup_path().startswith("/")
    assert pid_cgroup_path(os.getpid()) == self_cgroup_path()


def _fake_cgroupfs(tmp_path):
    base = tmp_path / "cf"
    base.mkdir()
    (base / "cgroup.controllers").write_text("cpuset cpu io memory\n")
    (base / "cgroup.subtree_control").write_text("cpuset cpu io memory\n")
    (base / "cgroup.procs").write_text("")
    (tmp_path / "cpuset.cpus.effective").write_text("0-3\n")
    return CgroupTree(str(tmp_path), "cf")


def test_tree_on_fake_fs_applies_and_clears_tiers(tmp_path):
    tree = _fake_cgroupfs(tmp_path)
    assert tree.available() and tree.writable() and tree.controllers() == ["cpuset", "cpu", "io", "memory"]
    tree.ensure("wechat")
    leaf = tmp_path / "cf" / "wechat"
    (leaf / "memory.current").write_text(str(900 * MB))
    (leaf / "memory.reclaim").write_text("")
    prof = ThrottleProfile(cpu_cores=1, mem_frac=0.5, io_weight=50)
    res = apply_tier(tree, "wechat", "shrunk", prof, 800 * MB)
    assert res["tier"] == "shrunk" and res["reclaim"] == "memory.reclaim"
    assert res["reclaim_bytes"] == 500 * MB                      # 900 current - 400 high
    assert (leaf / "cpu.max").read_text() == f"{PERIOD_US} {PERIOD_US}"
    assert (leaf / "memory.high").read_text() == str(400 * MB)
    assert (leaf / "memory.max").read_text() == "max"
    assert (leaf / "io.weight").read_text() == "50"
    assert (leaf / "memory.reclaim").read_text() == str(500 * MB)
    assert all(not v.startswith("ERR") for v in res["limits"].values()), res["limits"]

    res = apply_tier(tree, "wechat", "fg", prof)
    assert (leaf / "cpu.max").read_text() == f"max {PERIOD_US}"
    assert (leaf / "memory.high").read_text() == "max" and (leaf / "cpu.weight").read_text() == "100"
    assert res["reclaim"] is None
    assert tree.leaves() == ["wechat"]
    st = tree.stats("wechat")
    assert st["memory_current"] == 900 * MB and st["memory_high"] == "max" and st["n_procs"] == 0


def test_tree_reports_missing_controller_instead_of_failing(tmp_path):
    tree = _fake_cgroupfs(tmp_path)
    (tmp_path / "cf" / "cgroup.controllers").write_text("cpu memory\n")
    tree.ensure("qq")
    applied = tree.set_limits("qq", cpu_max="50000 100000", cpuset="3", memory_high="max")
    assert applied["cpu.max"] == "50000 100000"
    assert applied["cpuset.cpus"].startswith("ERR controller cpuset")
    assert tree.errors == []        # a disabled controller is not an I/O error


def test_leaf_of_and_inside(tmp_path, monkeypatch):
    tree = CgroupTree(str(tmp_path), "cf")
    import cf.linux.cgroup as cg
    monkeypatch.setattr(cg, "pid_cgroup_path", lambda pid: {1: "/cf/wechat", 2: "/cf/host", 3: "/user.slice"}[pid])
    assert tree.leaf_of(1) == "wechat" and tree.leaf_of(2) == "host" and tree.leaf_of(3) is None
    assert tree.inside(1) and not tree.inside(3)


REAL = CgroupTree()
real_ok = REAL.available() and REAL.writable()


@pytest.mark.skipif(not real_ok, reason="/sys/fs/cgroup/cf not delegated to this user (scripts/ubuntu/setup_cgroups.sh)")
def test_real_cgroup_tier_round_trip():
    """Needs a delegated tree; moves nothing, only creates an empty leaf and writes knobs."""
    name = f"pytest_{os.getpid()}"
    REAL.ensure(name)
    try:
        prof = ThrottleProfile(cpu_cores=1, mem_frac=0.5)
        res = apply_tier(REAL, name, "shrunk", prof, 600 * MB)
        assert res["limits"]["cpu.max"] == f"{PERIOD_US} {PERIOD_US}"
        assert REAL.read(name, "cpu.max") == f"{PERIOD_US} {PERIOD_US}"
        assert int(REAL.read(name, "memory.high")) == 300 * MB
        assert REAL.read(name, "memory.max") == "max"
        apply_tier(REAL, name, "fg", prof)
        assert REAL.read(name, "cpu.max") == f"max {PERIOD_US}" and REAL.read(name, "memory.high") == "max"
    finally:
        assert REAL.remove(name)
