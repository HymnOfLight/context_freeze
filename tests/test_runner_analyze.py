import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from fake_device import FakeDevice  # noqa: E402

from cf.analyze import load, summarize, pareto_rows  # noqa: E402
from cf.device import Device  # noqa: E402
from cf.runner import Experiment  # noqa: E402

APPS = [f"com.example.app{i}" for i in range(6)]


@pytest.fixture
def fast_cfg(tmp_path):
    return {"apps": APPS + ["com.missing.app"], "policy": "landlord", "eta": 0.3,
            "trace": {"kind": "zipf", "T": 12, "seed": 3}, "dwell_s": 0, "settle_s": 0,
            "warmup_dwell_s": 0, "zram_mb": 512, "out_dir": str(tmp_path), "name": "t"}


def run(cfg, policy):
    cfg = dict(cfg, policy=policy, name=f"{policy}_{cfg['eta']}")
    exp = Experiment(cfg, FakeDevice(APPS), log=lambda *a: None)
    return exp.run()


def test_runner_end_to_end(fast_cfg):
    path = run(fast_cfg, "landlord")
    recs = [json.loads(l) for l in open(path)]
    types = [r["type"] for r in recs]
    assert types[0] == "meta" and "budget" in types and types[-1] == "end"
    steps = [r for r in recs if r["type"] == "step"]
    assert len(steps) == 12
    meta = recs[0]
    assert "com.missing.app" not in meta["apps"]           # dropped with a warning
    # the requested app is never frozen / compressed after its launch
    for s in steps:
        assert s["req"] not in s["frozen"] and s["req"] not in s["compressed"]
        assert s["req"] in s["resident"]
    # some background app got compressed under a 30 % budget
    assert any(s["compressed"] for s in steps)
    assert any(s["actions"]["reclaim"] for s in steps)
    # the policy's model residency respects the budget (both in kB) unless even
    # "everything compressed" does not fit, in which case only r_t stays resident
    assert all(s["M_model_kb"] <= s["budget_kb"] + 1 or len(s["resident"]) == 1 for s in steps)


def test_generous_budget_keeps_some_apps_resident(fast_cfg):
    path = run(dict(fast_cfg, eta=0.7), "lru")
    steps = [json.loads(l) for l in open(path) if '"type": "step"' in l]
    assert any(len(s["resident"]) > 1 for s in steps)
    assert any(len(s["compressed"]) > 0 for s in steps)


def test_none_policy_touches_nothing(fast_cfg):
    cfg = dict(fast_cfg, freeze_resident=False, eta=1.0)
    path = run(cfg, "none")
    steps = [json.loads(l) for l in open(path) if '"type": "step"' in l]
    assert all(not s["frozen"] and not s["compressed"] for s in steps)


def test_analyze_summary(fast_cfg):
    p1 = run(fast_cfg, "landlord")
    p2 = run(dict(fast_cfg, eta=0.6), "lru")
    rows = [summarize(load(p)) for p in (p1, p2)]
    r = rows[0]
    assert r["policy"] == "landlord" and r["T"] == 12 and r["k"] == 6
    assert r["lat_p50_ms"] is not None and r["lat_p99_ms"] >= r["lat_p50_ms"]
    assert r["swap_write_mb"] is not None and r["swap_write_mb"] > 0
    assert r["n_hot"] + r["n_warm"] + r["n_cold"] == 12
    assert r["reclaim_methods"] == "memcg_v2.memory.reclaim"
    prow = pareto_rows(rows)
    assert len(prow) == 2 and all("mem_saving_pct" in x for x in prow)


def test_parse_sample_blob():
    """Exercise Device._parse_sample on a captured-style blob (no adb involved)."""
    dev = Device.__new__(Device)
    blob = (
        "##MEMINFO\nMemTotal:        3000000 kB\nMemAvailable:    1200000 kB\n"
        "##VMSTAT\npswpin 10\npswpout 20\n"
        "##PSI\nsome avg10=0.00 avg60=0.00 avg300=0.00 total=5\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=1\n"
        "##ZRAM\n1000 300 320 0 320 0 0 0 0\n"
        "##SWAPS\nFilename Type Size Used Priority\n/dev/block/zram0 partition 1048572 100 32767\n"
        "##PS\nPID UID NAME\n1 0 init\n500 10101 com.foo\n501 10101 com.foo:remote\n600 10102 com.bar\n"
        "##PID 500 10101\nRss: 100 kB\nPss: 90 kB\nPss_Anon: 60 kB\nPss_File: 30 kB\nSwapPss: 5 kB\n"
        "##STAT\n500 (com.foo) S 1 1 0 0 -1 0 1 0 7 0 0 0 0 0 20 0 1 0 1 1 1 1\n##OOM\n900\n##FRZ\n1\n"
        "##PID 501 10101\nRss: 10 kB\nPss: 8 kB\nPss_Anon: 4 kB\nPss_File: 4 kB\nSwapPss: 0 kB\n"
        "##STAT\n501 (com.foo:remote) S 1 1 0 0 -1 0 1 0 1 0 0 0 0 0 20 0 1 0 1 1 1 1\n##OOM\n905\n##FRZ\n0\n"
        "##PID 600 10102\n"   # process vanished before cat -> no smaps -> skipped
        "##STAT\n##OOM\n##FRZ\n"
    )
    res = dev._parse_sample(blob, ["com.foo", "com.bar"], {"com.foo": 10101, "com.bar": 10102})
    foo = res["apps"]["com.foo"].to_dict()
    assert foo["pss_kb"] == 98 and foo["pss_anon_kb"] == 64 and foo["swap_pss_kb"] == 5
    assert foo["majflt"] == 8 and foo["frozen"] is True and sorted(foo["pids"]) == [500, 501]
    assert not res["apps"]["com.bar"].alive
    assert res["system"]["zram"]["mem_used_total"] == 320
    assert res["system"]["vmstat"]["pswpout"] == 20
    assert res["system"]["swaps"][0]["priority"] == 32767
