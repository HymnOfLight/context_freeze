"""Ubuntu throttle runner end-to-end on the fake desktop: tiers instead of freezes, nothing is
killed, WARM switches after shrink, checkpoint / resume, analyzer rows, state_lost detection."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from fake_linux_desktop import OVERRIDES, FakeLinuxDesktop  # noqa: E402

from cf.analyze import aggregate, load  # noqa: E402
from cf.linux.analyze import EXTRA_AGG_COLS, summarize  # noqa: E402
from cf.linux.runner import ThrottleExperiment  # noqa: E402
from cf.runner import ResumeError  # noqa: E402

APPS = list(OVERRIDES)
T = 16


def cfg_for(tmp_path, policy="static", **kw):
    cfg = {"apps": APPS, "app_overrides": OVERRIDES, "policy": policy, "eta": 0.3,
           "trace": {"kind": "zipf", "T": T, "seed": 3}, "dwell_s": 0.03, "settle_s": 0, "warmup_dwell_s": 0,
           "out_dir": str(tmp_path), "name": f"{policy}_linux", "min_ram_mb": 1024}
    cfg.update(kw)
    return cfg


def records(path):
    return [json.loads(l) for l in open(path) if l.strip()]


def steps_of(path):
    return [r for r in records(path) if r["type"] == "step"]


def test_static_policy_only_foreground_is_free(tmp_path):
    cfg = cfg_for(tmp_path, "static")
    dev = FakeLinuxDesktop(APPS, seed=1)
    path = ThrottleExperiment(cfg, dev, log=lambda *a: None).run()
    recs = records(path)
    meta = next(r for r in recs if r["type"] == "meta")
    assert meta["backend"] == "linux" and meta["categories"]["fake_dl"] == "download"
    steps = steps_of(path)
    assert len(steps) == T
    for s in steps:
        assert s["resident"] == [s["req"]]
        others = [a for a in APPS if a != s["req"]]
        assert all(s["tiers"][a] == "shrunk" for a in others)
        assert s["tiers"][s["req"]] == "fg"
        assert s["killed_since_prev"] == [] and s["state_lost"] == {}
        # Android-compatible aliases so cf.analyze works unchanged
        assert s["frozen"] == s["throttled"] and s["compressed"] == s["shrunk"]
        assert s["was_compressed"] == s["was_shrunk"]
    # every app survived the whole run with its original pid (no restarts, no kills)
    assert all(dev.state[a]["pid"] for a in APPS)
    assert sum(1 for s in steps if s["launch"]["launch_state"] == "COLD") == 0
    # a switch back to a shrunk app is WARM and slower than a resident HOT switch
    warm = [s["launch"]["total_time_ms"] for s in steps if s["launch"]["launch_state"] == "WARM"]
    assert warm and max(warm) > 40      # small apps fit under mem_min_mb and lose nothing
    assert recs[-1]["type"] == "end"
    assert dev.tiers == {a: "fg" for a in APPS}          # cleanup lifted every limit


@pytest.mark.parametrize("policy", ["lru", "landlord", "hybrid"])
def test_budgeted_policy_keeps_residents_cpu_limited_only(tmp_path, policy):
    # the shrunk footprint of an app is its memory.high; with the default 192 MB floor and
    # 150..450 MB apps the floor alone would use up the budget, so lower it for the model
    cfg = cfg_for(tmp_path, policy, eta=0.6, throttle={"mem_min_mb": 32})
    dev = FakeLinuxDesktop(APPS, seed=2)
    path = ThrottleExperiment(cfg, dev, log=lambda *a: None).run()
    steps = steps_of(path)
    seen_bg = False
    for s in steps:
        for a in APPS:
            if a == s["req"]:
                assert s["tiers"][a] == "fg"
            elif a in s["resident"]:
                assert s["tiers"][a] == "bg"
                seen_bg = True
            else:
                assert s["tiers"][a] == "shrunk"
        # memory budget accounting still works through the Android-style keys
        assert "M_bg_kb" in s and s["budget_kb"] > 0
    assert seen_bg, "with eta=0.6 some app must stay resident"
    row = summarize(load(path))
    assert row["policy"] == policy and row["T"] == T
    # 5 background apps, each capped at cpu_cores=1 (the compute apps would take 2.5 each)
    assert row["bg_cores_avg"] is not None and row["bg_cores_avg"] < 5 * 1.0 * 1.3
    for s in steps:
        for a, b in s["bg"].items():
            if a != s["req"] and b["tier"] in ("bg", "shrunk") and b["cores"] is not None:
                assert b["cores"] < 1.0 * 1.5
    assert row["bg_throttled_pct"] > 0
    assert row["state_lost_events"] == 0 and row["oom_kills"] == 0


def test_cpu_throttle_resident_false_leaves_residents_untouched(tmp_path):
    cfg = cfg_for(tmp_path, "lru", eta=0.7, cpu_throttle_resident=False, throttle={"mem_min_mb": 32})
    path = ThrottleExperiment(cfg, FakeLinuxDesktop(APPS, seed=4), log=lambda *a: None).run()
    tiers = {t for s in steps_of(path) for a, t in s["tiers"].items() if a in s["resident"]}
    assert tiers == {"fg"}


def test_never_throttle_app_stays_fg(tmp_path):
    cfg = cfg_for(tmp_path, "static", never_throttle=["fake_push"])
    path = ThrottleExperiment(cfg, FakeLinuxDesktop(APPS, seed=5), log=lambda *a: None).run()
    assert all(s["tiers"]["fake_push"] == "fg" for s in steps_of(path))


def test_metrics_per_category(tmp_path):
    cfg = cfg_for(tmp_path, "static")
    path = ThrottleExperiment(cfg, FakeLinuxDesktop(APPS, seed=6), log=lambda *a: None).run()
    row = summarize(load(path))
    assert row["download_mb"] > 0 and row["download_mbps"] > 0
    assert row["compute_cpu_s"] > 0
    assert row["notif_total"] >= 0 and row["notif_per_h"] is not None
    assert row["n_warm"] + row["n_hot"] + row["n_front"] + row["n_cold"] == T
    assert row["ping_shrunk_p50_ms"] > row["ping_resident_p50_ms"]
    assert row["cpu"] == "1c" and row["mem_frac"] == 0.5
    # the Linux columns ride along in aggregate()
    agg = aggregate([row], extra_cols=EXTRA_AGG_COLS)
    assert len(agg) == 1 and agg[0]["bg_cores_avg"] == pytest.approx(row["bg_cores_avg"], abs=0.1)


def test_state_lost_detects_crash_and_oom(tmp_path):
    cfg = cfg_for(tmp_path, "static")
    dev = FakeLinuxDesktop(APPS, seed=7)
    exp = ThrottleExperiment(cfg, dev, log=lambda *a: None)
    # inject during the run: crash one background app at step 3, OOM-kill event at step 6
    orig_step = exp.step

    def step(t, req):
        if t == 3:
            dev.crash_app = next(a for a in APPS if a != req)
        if t == 6:
            dev.oom_app = next(a for a in APPS if a != req)
        return orig_step(t, req)
    exp.step = step
    path = exp.run()
    steps = steps_of(path)
    crashed = [s for s in steps if any("all_processes_gone" in v for v in s["state_lost"].values())]
    oom = [s for s in steps if any("oom_kill" in v for v in s["state_lost"].values())]
    assert len(crashed) == 1 and crashed[0]["t"] == 3
    assert len(oom) == 1 and oom[0]["t"] == 6
    row = summarize(load(path))
    assert row["state_lost_events"] == 2 and row["oom_kills"] == 1
    # the crashed app is restarted COLD when the trace asks for it again
    assert any(s["launch"]["launch_state"] == "COLD" for s in steps if s["t"] > 3)


def test_short_lived_helper_process_is_not_state_loss():
    from cf.linux.device import LinuxDesktop
    before = {"pids": [10, 11, 12], "stable_pids": [10, 11], "window": 7, "cg_events": {"oom_kill": 0}}
    after = {"pids": [10, 11, 13], "stable_pids": [10, 11], "window": 7, "cg_events": {"oom_kill": 0}}
    assert LinuxDesktop.state_check(before, after) == {}
    after["pids"] = [10, 13]
    assert LinuxDesktop.state_check(before, after) == {"processes_gone": [11]}
    after.update(pids=[], window=None, cg_events={"oom_kill": 1})
    lost = LinuxDesktop.state_check(before, after)
    assert lost["all_processes_gone"] and lost["window_gone"] and lost["oom_kill"] == 1


def test_crash_then_resume_reapplies_tiers(tmp_path):
    cfg = cfg_for(tmp_path, "lru", eta=0.5)
    dev = FakeLinuxDesktop(APPS, seed=8)
    dev.fail_at_launch = len(APPS) + 7            # warmup + 6 steps, die on the 7th
    exp = ThrottleExperiment(cfg, dev, log=lambda *a: None)
    with pytest.raises(OSError):
        exp.run()
    path = os.path.join(str(tmp_path), cfg["name"] + ".jsonl")
    assert [s["t"] for s in steps_of(path)] == list(range(6))
    assert os.path.exists(path[:-6] + ".ckpt")
    ckpt_tiers = next(r for r in reversed(records(path)) if r["type"] == "step")["tiers"]

    dev2 = FakeLinuxDesktop(APPS, seed=9)
    for a in APPS:                                  # the apps are still running after the crash
        dev2.state[a]["pid"] = dev.state[a]["pid"]
        dev2.state[a]["window"] = dev.state[a]["window"]
    reapplied = {}
    orig_set_tier = dev2.set_tier

    def spy(id_, tier, m_fg_bytes=None):
        reapplied.setdefault(id_, tier)             # first call per app = the resume re-application
        return orig_set_tier(id_, tier, m_fg_bytes)
    dev2.set_tier = spy
    exp2 = ThrottleExperiment({"apps": APPS, "app_overrides": OVERRIDES, "out_dir": str(tmp_path),
                               "name": cfg["name"]}, dev2, log=lambda *a: None, resume=True)
    out = exp2.run()
    assert out == path
    recs = records(path)
    types = [r["type"] for r in recs]
    assert types.count("meta") == 1 and "resume" in types and types[-1] == "end"
    assert [s["t"] for s in steps_of(path)] == list(range(T))
    # the tiers from the checkpoint were re-applied before the first resumed step
    assert steps_of(path)[6]["t"] == 6
    for a, tier in ckpt_tiers.items():
        if tier != "fg":
            assert reapplied[a] == tier
    assert exp2.pol is not None and exp2.t_done == T - 1
    assert not os.path.exists(path[:-6] + ".ckpt")
    row = summarize(load(path))
    assert row["resumes"] == 1 and row["finished"] is True


def test_android_checkpoint_is_rejected(tmp_path):
    import pickle
    name = "mixed"
    path = os.path.join(str(tmp_path), name + ".ckpt")
    with open(path, "wb") as f:
        pickle.dump({"version": 1, "name": name, "cfg": {}}, f)
    with open(os.path.join(str(tmp_path), name + ".jsonl"), "w") as f:
        f.write(json.dumps({"type": "meta"}) + "\n")
    exp = ThrottleExperiment({"apps": APPS, "app_overrides": OVERRIDES, "out_dir": str(tmp_path), "name": name},
                             FakeLinuxDesktop(APPS), log=lambda *a: None, resume=True)
    with pytest.raises(ResumeError):
        exp.run()


def test_none_policy_is_a_true_baseline(tmp_path):
    cfg = cfg_for(tmp_path, "none", eta=1.0)
    path = ThrottleExperiment(cfg, FakeLinuxDesktop(APPS, seed=2), log=lambda *a: None).run()
    for s in steps_of(path):
        assert set(s["tiers"].values()) <= {"fg"} and s["throttled"] == [] and s["shrunk"] == []
        assert s["actions"]["tiers"] == {}
    row = summarize(load(path))
    assert row["bg_throttled_pct"] in (None, 0.0) and row["n_warm"] == 0


def test_dwell_range_and_timeline_sampling(tmp_path):
    cfg = cfg_for(tmp_path, "static", dwell_s=[0.2, 0.3], sample_every_s=0.05, trace={"kind": "zipf", "T": 4, "seed": 3})
    path = ThrottleExperiment(cfg, FakeLinuxDesktop(APPS, seed=2), log=lambda *a: None).run()
    steps = steps_of(path)
    assert all(0.2 <= s["dwell_s"] <= 0.3 for s in steps) and len({s["dwell_s"] for s in steps}) > 1
    assert all(len(s["timeline"]) >= 2 for s in steps)
    pt = steps[0]["timeline"][0]
    assert set(pt) == {"dt", "system", "apps"} and set(pt["apps"]) == set(APPS)
    assert "cg_mem_mb" in pt["apps"][APPS[0]] and "tier" in pt["apps"][APPS[0]]
    assert "pswpout" in pt["system"] and "psi_cpu" in pt["system"]
    # same seed -> same dwell draw per step (resume-safe)
    cfg2 = dict(cfg, name="again")
    path2 = ThrottleExperiment(cfg2, FakeLinuxDesktop(APPS, seed=2), log=lambda *a: None).run()
    assert [s["dwell_s"] for s in steps_of(path2)] == [s["dwell_s"] for s in steps]


def test_placeholder_fill_runs_uninstalled_apps_as_placeholders(tmp_path, monkeypatch):
    from cf.linux import apps as A
    monkeypatch.setattr(A, "is_installed", lambda a, arch=None, mobile=None: a.kind == "placeholder" or a.id in OVERRIDES)
    monkeypatch.setattr(A, "EXTRA_NAMES", dict(A.EXTRA_NAMES))      # placeholder display names must not leak
    cfg = cfg_for(tmp_path, "static", apps=APPS[:2] + ["wechat", "jianying", "zwcad"], placeholder_fill=True,
                  trace={"kind": "scenario", "scenario": {"name": "x", "title": "x", "apps": {"wechat": 3, "jianying": 2,
                         "zwcad": 1, APPS[0]: 1, APPS[1]: 1}, "hub": "wechat", "flows": [["wechat", "jianying"]],
                         "p_hub": .3, "p_flow": .3, "stickiness": .2, "description": ""}, "T": 6, "seed": 1})

    class Dev(FakeLinuxDesktop):
        def is_installed(self, a):
            return A.is_installed(a)

    exp = ThrottleExperiment(cfg, Dev(APPS[:2] + ["wechat", "jianying", "zwcad"], seed=1), log=lambda *a: None)
    path = exp.run()
    meta = records(path)[0]
    assert sorted(meta["placeholders"]) == ["jianying", "wechat", "zwcad"]
    assert meta["kinds"]["wechat"] == "placeholder" and meta["kinds"][APPS[0]] == "desktop"
    assert exp.dev.cat["wechat"].cmd.startswith("python3 -m cf.linux.placeholder_app --app wechat ")
    assert "--notify-every" in exp.dev.cat["wechat"].cmd and "--io-mbps" not in exp.dev.cat["wechat"].cmd
    assert "--workers 3" in exp.dev.cat["jianying"].cmd
    assert set(meta["apps"]) == set(APPS[:2]) | {"wechat", "jianying", "zwcad"}   # scenario ids unchanged
    assert summarize(load(path))["n_placeholders"] == 3
    assert A.CATALOG["wechat"].kind == "desktop"                                  # global catalogue untouched


def test_per_app_rows_report_and_plots(tmp_path):
    from cf.linux.analyze import per_app_rows, PER_APP_COLS, main as analyze_main
    paths = []
    for pol, eta in (("none", 1.0), ("static", 0.0), ("lru", 0.5)):
        cfg = cfg_for(tmp_path, pol, eta=eta, name=f"{pol}_r", sample_every_s=0.01, dwell_s=0.06)
        paths.append(ThrottleExperiment(cfg, FakeLinuxDesktop(APPS, seed=4), log=lambda *a: None).run())
    rows = per_app_rows(load(paths[1]))
    assert [r["app"] for r in rows] == APPS and all(set(PER_APP_COLS) <= set(r) for r in rows)
    static_dl = next(r for r in rows if r["category"] == "download")
    assert static_dl["t_shrunk_s"] >= 0 and static_dl["write_mb"] >= 0 and static_dl["state_lost"] == 0
    assert any(r["lat_shrunk_p50_ms"] for r in rows)        # some app came back from shrunk
    none_rows = per_app_rows(load(paths[0]))
    assert all(r["cores_bg_avg"] is None and r["t_bg_s"] == 0 for r in none_rows)
    pytest.importorskip("matplotlib")
    out = tmp_path / "plots"
    analyze_main(paths + ["--per-app", str(tmp_path / "per_app.csv"), "--plots-dir", str(out), "--md", str(tmp_path / "r.md")])
    made = sorted(p.name for p in out.iterdir())
    assert "latency_cdf.png" in made and "bg_tradeoff.png" in made and "static_r.png" in made and "static_r_timeline.png" in made
    md = (tmp_path / "r.md").read_text()
    assert "## Per run" in md and "## Per app" in md and "latency_cdf.png" in md and "state_lost_events" in md
    assert (tmp_path / "per_app.csv").read_text().splitlines()[0].startswith("run,app,kind")
