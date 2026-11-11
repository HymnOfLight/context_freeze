"""cgroup v2 tier logic: the knob values each tier writes (pure), the tree on a fake cgroupfs
(a plain directory), and - when /sys/fs/cgroup/cf is delegated to us - the real thing."""
import os
import time

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
    assert (leaf / "cpuset.cpus").read_text() == "0-3" and res["limits"]["cpuset.cpus"] == "0-3"   # every CPU again
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
        # pinned to one core in the background, on all of them again in the foreground
        if "cpuset" in REAL.controllers() and len(REAL.effective_cpus()) > 1:
            pin = ThrottleProfile(cpu_mode="both", cpuset_cpus="last", cpu_cores=0.1)
            last = str(REAL.effective_cpus()[-1])
            apply_tier(REAL, name, "bg", pin)
            assert REAL.read(name, "cpuset.cpus") == last and REAL.read(name, "cpuset.cpus.effective") == last
            res = apply_tier(REAL, name, "fg", pin)
            assert not res["limits"]["cpuset.cpus"].startswith("ERR"), res["limits"]
            assert REAL.read(name, "cpuset.cpus.effective") == REAL.read("", "cpuset.cpus.effective")
    finally:
        assert REAL.remove(name)


def test_an_empty_value_is_written_as_an_empty_line(tmp_path):
    """open(...).write("") issues no write() at all - the kernel would never see the request."""
    tree = _fake_cgroupfs(tmp_path)
    tree.ensure("qq")
    (tmp_path / "cf" / "qq" / "knob").write_text("old")
    assert tree.write("qq", "knob", "") is None
    assert (tmp_path / "cf" / "qq" / "knob").read_text() == "\n"


def test_unpin_gives_every_cpu_back_and_falls_back_to_inherit(tmp_path):
    tree = _fake_cgroupfs(tmp_path)
    tree.ensure("qq")
    leaf = tmp_path / "cf" / "qq"
    assert tree.set_limits("qq", cpuset="3") == {"cpuset.cpus": "3"}
    assert tree.set_limits("qq", cpuset="") == {"cpuset.cpus": "0-3"} and (leaf / "cpuset.cpus").read_text() == "0-3"
    assert tree.clear_limits("qq")["cpuset.cpus"] == "0-3"

    class Refusing(CgroupTree):                                   # a kernel that takes no explicit list here
        def write(self, name, file, value):
            if file == "cpuset.cpus" and value:
                self.errors.append("refused")
                return "x: EINVAL Invalid argument"
            return super().write(name, file, value)
    odd = Refusing(str(tmp_path), "cf")
    assert odd.set_limits("qq", cpuset="") == {"cpuset.cpus": ""} and odd.errors == []
    assert (leaf / "cpuset.cpus").read_text() == "\n"


@pytest.mark.skipif(not real_ok, reason="/sys/fs/cgroup/cf not delegated to this user (scripts/ubuntu/setup_cgroups.sh)")
def test_real_cgroup_lets_a_running_process_back_on_every_cpu():
    """The case that matters: the cgroup has a process in it.  The kernel refuses an empty
    cpuset.cpus there (ENOSPC), so an app that had been pinned to the background's core stayed on
    it in the foreground.  Needs the test to run inside the tree (source scripts/ubuntu/cf_enter.sh)."""
    import subprocess
    if "cpuset" not in REAL.controllers() or len(REAL.effective_cpus()) < 2:
        pytest.skip("needs the cpuset controller and two CPUs")
    name = f"pytest_pin_{os.getpid()}"
    REAL.ensure(name)
    p = subprocess.Popen(["sleep", "30"])
    try:
        ok, failed = REAL.attach(name, [p.pid])
        if not ok:
            REAL.errors.clear()
            pytest.skip(f"cannot move a process into the tree from here ({list(failed.values())[0]})")
        every, last = set(REAL.effective_cpus()), REAL.effective_cpus()[-1]
        pin = ThrottleProfile(cpu_mode="both", cpuset_cpus="last", cpu_cores=0.1)
        apply_tier(REAL, name, "bg", pin)
        assert os.sched_getaffinity(p.pid) == {last}
        res = apply_tier(REAL, name, "fg", pin)
        assert not res["limits"]["cpuset.cpus"].startswith("ERR"), res["limits"]
        assert os.sched_getaffinity(p.pid) == every
        assert REAL.read(name, "cpuset.cpus.effective") == REAL.read("", "cpuset.cpus.effective")
        apply_tier(REAL, name, "shrunk", pin, 64 * MB)
        assert os.sched_getaffinity(p.pid) == {last}
        REAL.clear_limits(name)
        assert os.sched_getaffinity(p.pid) == every and REAL.errors == []
    finally:
        p.kill()
        p.wait()
        for _ in range(50):
            if REAL.remove(name):
                break
            time.sleep(0.05)


def test_squeeze_profile_asks_for_the_whole_cgroup_several_times(tmp_path):
    from cf.linux.cgroup import squeeze
    tree = _fake_cgroupfs(tmp_path)
    tree.ensure("qq")
    leaf = tmp_path / "cf" / "qq"
    (leaf / "memory.current").write_text(str(900 * MB))
    (leaf / "memory.reclaim").write_text("")
    prof = ThrottleProfile(cpu_mode="both", cpu_cores=0.1, cpu_weight=1, mem_frac=0.1, mem_min_mb=64, squeeze_rounds=3)
    assert "squeeze x3" in prof.describe()
    res = apply_tier(tree, "qq", "shrunk", prof, 800 * MB)
    assert (leaf / "cpu.max").read_text() == f"{PERIOD_US // 10} {PERIOD_US}"
    assert (leaf / "cpu.weight").read_text() == "1"
    assert (leaf / "memory.high").read_text() == str(80 * MB)
    # the fake fs never lowers memory.current, so every round asks for all 900 MB again
    assert res["squeeze"]["rounds"] == 3 and res["reclaim_bytes"] == 3 * 900 * MB
    assert res["reclaim"] == "memory.reclaim x3" and (leaf / "memory.reclaim").read_text() == str(900 * MB)
    (leaf / "memory.current").write_text("0")
    assert squeeze(tree, "qq", 3) == {"how": "nothing", "rounds": 0, "asked_bytes": 0, "before_bytes": 0, "after_bytes": 0}
    st = tree.stats("qq")
    assert "pgsteal" in st and "pgscan" in st


def test_reclaim_swappiness_argument_falls_back_once_on_old_kernels(tmp_path):
    tree = _fake_cgroupfs(tmp_path)
    tree.ensure("wps")
    leaf = tmp_path / "cf" / "wps"
    (leaf / "memory.reclaim").write_text("")
    assert tree.reclaim("wps", 10 * MB, swappiness=200) == "memory.reclaim"
    assert (leaf / "memory.reclaim").read_text() == f"{10 * MB} swappiness=200"
    # a kernel < 6.9 rejects the argument with EINVAL: plain reclaim, and no second attempt
    real = tree.write
    tries = []
    def write(name, file, value):
        tries.append(value)
        if "swappiness" in value:
            tree.errors.append("EINVAL"); return "EINVAL"
        return real(name, file, value)
    tree.write = write
    assert tree.reclaim("wps", 20 * MB, swappiness=200) == "memory.reclaim"
    assert tree.reclaim("wps", 30 * MB, swappiness=200) == "memory.reclaim"
    assert tries == [f"{20 * MB} swappiness=200", str(20 * MB), str(30 * MB)] and tree.errors == []
    assert (leaf / "memory.reclaim").read_text() == str(30 * MB)
    prof = ThrottleProfile(squeeze_rounds=2, reclaim_swappiness=200)
    assert prof.describe().endswith("squeeze x2 swappiness=200")
