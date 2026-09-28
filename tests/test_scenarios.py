"""Mainland-China app scenarios: restriction to available apps, structure of the sampled traces,
day schedule segments, integration with the runner and the analysis."""
import json
import os
import sys
from collections import Counter

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from fake_device import FakeDevice  # noqa: E402

from cf import scenarios as S  # noqa: E402
from cf.analyze import load, segment_rows, summarize  # noqa: E402
from cf.runner import Experiment  # noqa: E402
from cf.trace import make_trace, make_trace_meta  # noqa: E402

CN14 = json.load(open(os.path.join(os.path.dirname(__file__), "..", "configs", "cn_apps.json")))["apps"]


def test_catalog_and_scenarios_are_consistent():
    for sc in S.SCENARIOS.values():
        assert sc.hub in sc.apps
        for a in sc.apps:
            assert a in S.CATALOG, a
        for f in sc.flows:
            assert len(f) >= 2 and all(a in sc.apps for a in f)
        assert sc.p_hub + sc.p_flow + sc.stickiness < 1
    assert abs(sum(s for _, s in S.DAY_SCHEDULE) - 1) < 1e-9
    # every scenario keeps >= 4 apps with the default 14-app config
    for name in S.SCENARIOS:
        sc, dropped = S.restrict(S.SCENARIOS[name], CN14)
        assert len(sc.apps) >= 4, (name, sc.apps)
        assert sc.hub in sc.apps
        assert all(a in CN14 for a in sc.apps) and all(a not in CN14 for a in dropped)


def test_restrict_drops_missing_apps_and_repairs_flows():
    sc, dropped = S.restrict(S.SCENARIOS["office"], [S.MM, S.WPS, S.MAIL163])
    assert set(sc.apps) == {S.MM, S.WPS, S.MAIL163} and S.DINGTALK in dropped
    assert [S.MAIL163, S.WPS, S.MAIL163] in sc.flows
    assert all(len(f) >= 2 and all(a in sc.apps for a in f) for f in sc.flows)
    # hub falls back to the most popular available app
    sc2, _ = S.restrict(S.SCENARIOS["commute"], [S.MM, S.AMAP, S.ALIPAY])
    assert sc2.hub == S.MM
    with pytest.raises(ValueError):
        S.restrict(S.SCENARIOS["shopping"], [S.TAOBAO])
    with pytest.raises(ValueError):
        S.scenario_from("nightlife")


@pytest.mark.parametrize("name", list(S.SCENARIOS))
def test_scenario_trace_structure(name):
    T = 300
    tr = S.gen_scenario(CN14, T, name, seed=3)
    assert len(tr) == T and set(tr) <= set(CN14)
    assert all(a != b for a, b in zip(tr, tr[1:])), "no request repeats the foreground app"
    sc, _ = S.restrict(S.SCENARIOS[name], CN14)
    cnt = Counter(tr)
    assert cnt[sc.hub] == max(cnt.values()), "the hub is the most requested app"
    assert len(cnt) >= min(4, len(sc.apps))
    # at least one fixed flow appears verbatim
    joined = " ".join(tr)
    assert any(" ".join(f) in joined for f in sc.flows)
    assert S.gen_scenario(CN14, T, name, seed=3) == tr           # deterministic
    assert S.gen_scenario(CN14, T, name, seed=4) != tr


def test_inline_scenario_spec():
    spec = {"name": "mine", "apps": {S.MM: 3, S.QQ: 1, S.WEIBO: 1}, "flows": [[S.QQ, S.WEIBO]]}
    tr = S.gen_scenario([S.MM, S.QQ, S.WEIBO, S.BILI], 50, spec, seed=1)
    assert set(tr) == {S.MM, S.QQ, S.WEIBO}
    assert S.scenario_from(spec).hub == S.MM


def test_day_segments_cover_T_and_follow_schedule():
    tr, segs = S.gen_day(CN14, 100, seed=2)
    assert len(tr) == 100
    assert [g["scenario"] for g in segs] == [n for n, _ in S.DAY_SCHEDULE]
    assert segs[0]["start"] == 0 and segs[-1]["end"] == 100
    assert all(a["end"] == b["start"] for a, b in zip(segs, segs[1:]))
    assert [g["end"] - g["start"] for g in segs] == [15, 40, 15, 10, 20]
    assert all(a != b for a, b in zip(tr, tr[1:]))                    # also across segment borders
    tr2, segs2 = S.gen_day(CN14, 7, [("office", 1), ("social", 1)], seed=0)
    assert len(tr2) == 7 and [g["end"] - g["start"] for g in segs2] == [4, 3]


def test_make_trace_meta_and_dropped():
    apps = [S.MM, S.WPS, S.MAIL163, S.DINGTALK]
    tr, meta = make_trace_meta(apps, {"kind": "scenario", "scenario": "office", "T": 20, "seed": 1})
    assert len(tr) == 20 and meta["scenario"] == "office" and meta["segments"] == [{"scenario": "office", "start": 0, "end": 20}]
    assert S.WEMEET in meta["dropped"]
    assert make_trace(apps, {"kind": "scenario", "scenario": "office", "T": 20, "seed": 1}) == tr
    tr, meta = make_trace_meta(CN14, {"kind": "day", "T": 30, "seed": 1})
    assert len(tr) == 30 and len(meta["segments"]) == 5 and meta["scenario"] == "day"
    assert make_trace_meta(CN14, {"kind": "zipf", "T": 5})[1] == {}


def test_runner_day_trace_records_segments_and_analyze_breaks_them_down(tmp_path):
    cfg = {"apps": CN14, "policy": "landlord", "eta": 0.3, "trace": {"kind": "day", "T": 25, "seed": 1},
           "dwell_s": 0, "settle_s": 0, "warmup_dwell_s": 0, "zram_mb": 512, "out_dir": str(tmp_path), "name": "day"}
    exp = Experiment(cfg, FakeDevice(CN14, seed=1), log=lambda *a: None)
    path = exp.run()
    recs = [json.loads(l) for l in open(path) if l.strip()]
    tr = next(r for r in recs if r["type"] == "trace")
    assert tr["scenario"] == "day" and len(tr["segments"]) == 5 and "dropped" not in tr
    run = load(path)
    assert run["segments"] == tr["segments"]
    row = summarize(run)
    assert row["scenario"] == "day" and row["T"] == 25
    seg = segment_rows(run)
    assert [g["segment"] for g in seg] == ["commute", "office", "social", "shopping", "evening"]
    assert sum(g["steps"] for g in seg) == 25
    assert all(g["n_hot"] + g["n_warm"] + g["n_cold"] == g["steps"] for g in seg)
    # single-scenario run: scenario column, no segment table
    cfg2 = dict(cfg, name="office", trace={"kind": "scenario", "scenario": "social", "T": 10, "seed": 1})
    run2 = load(Experiment(cfg2, FakeDevice(CN14, seed=1), log=lambda *a: None).run())
    assert summarize(run2)["scenario"] == "social" and segment_rows(run2) == []
