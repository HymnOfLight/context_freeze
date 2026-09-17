"""Strict preflight, fixed budget denominator, kill-reason capture and replicate aggregation."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from fake_device import FakeDevice  # noqa: E402

from cf.analyze import aggregate, load, pareto_rows, summarize  # noqa: E402
from cf.runner import Experiment, PreflightError  # noqa: E402

APPS = [f"com.example.app{i}" for i in range(6)]


def cfg_for(tmp_path, **kw):
    c = {"apps": APPS, "policy": "lru", "eta": 0.4, "trace": {"kind": "zipf", "T": 10, "seed": 1},
         "dwell_s": 0, "settle_s": 0, "warmup_dwell_s": 0, "zram_mb": 512, "out_dir": str(tmp_path), "name": "p"}
    c.update(kw)
    return c


def test_strict_preflight_aborts_on_system_freezer_and_software_gl(tmp_path):
    dev = FakeDevice(APPS)
    dev.system_freezer = "null"
    exp = Experiment(cfg_for(tmp_path), dev, log=lambda *a: None)
    with pytest.raises(PreflightError):
        exp.run()
    assert not os.path.exists(os.path.join(str(tmp_path), "p.jsonl"))    # no empty result left behind
    dev = FakeDevice(APPS)
    dev.gles = "GLES: Google (Google Inc. (Google)), Android Emulator OpenGL ES Translator (ANGLE (SwiftShader Device))"
    with pytest.raises(PreflightError):
        Experiment(cfg_for(tmp_path), dev, log=lambda *a: None).run()
    # --no-strict: warns and runs
    exp = Experiment(cfg_for(tmp_path, strict_preflight=False), dev, log=lambda *a: None)
    path = exp.run()
    assert any("software GPU" in w for w in exp.log.warnings)
    assert Experiment.jsonl_status(path)["finished"]


def test_fixed_m_fg_gives_identical_budget_across_runs(tmp_path):
    p1 = Experiment(cfg_for(tmp_path, name="a"), FakeDevice(APPS, seed=1), log=lambda *a: None).run()
    b1 = [json.loads(l) for l in open(p1) if '"type": "budget"' in l][0]
    assert b1["m_fg_source"] == "measured"
    # a different device (different app sizes) but the same fixed denominator
    p2 = Experiment(cfg_for(tmp_path, name="b", fixed_m_fg_kb=b1["m_fg_kb"], fixed_a_fg_kb=b1["a_fg_kb"]),
                    FakeDevice(APPS, seed=99), log=lambda *a: None).run()
    b2 = [json.loads(l) for l in open(p2) if '"type": "budget"' in l][0]
    assert b2["m_fg_source"] == "fixed" and b2["budget_kb"] == b1["budget_kb"]
    assert summarize(load(p2))["m_fg_source"] == "fixed"


def test_kill_reasons_recorded(tmp_path):
    dev = FakeDevice(APPS, seed=2)
    dev.kill_every = 4
    path = Experiment(cfg_for(tmp_path, name="k"), dev, log=lambda *a: None).run()
    steps = [json.loads(l) for l in open(path) if '"type": "step"' in l]
    killed = [(s["t"], k, s["kill_info"].get(k)) for s in steps for k in s["killed_since_prev"]]
    assert killed, "kill injection did not produce a killed app"
    assert all(info and info["description"] == "bg anr" for _, _, info in killed)
    row = summarize(load(path))
    assert row["n_killed"] == len(killed) and row["kill_reasons"].startswith("bg anr:")


def test_aggregate_replicates_and_separates_guest_ram(tmp_path):
    rows = []
    for seed in (1, 2, 3):
        d = FakeDevice(APPS, seed=seed)
        cfg = cfg_for(tmp_path, name=f"lru_s{seed}", trace={"kind": "zipf", "T": 10, "seed": seed})
        rows.append(summarize(load(Experiment(cfg, d, log=lambda *a: None).run())))
    d = FakeDevice(APPS, seed=7)
    d.mem_total_mb = 6 * 1024
    rows.append(summarize(load(Experiment(cfg_for(tmp_path, name="lru_6g"), d, log=lambda *a: None).run())))
    for seed in (1, 2):
        d = FakeDevice(APPS, seed=seed)
        cfg = cfg_for(tmp_path, name=f"none_s{seed}", policy="none", eta=1.0, freeze_resident=False,
                      trace={"kind": "zipf", "T": 10, "seed": seed})
        rows.append(summarize(load(Experiment(cfg, d, log=lambda *a: None).run())))
    assert {r["seed"] for r in rows} == {1, 2, 3} and {r["guest_ram_mb"] for r in rows} == {3072, 6144}
    agg = aggregate(rows)
    keys = {(a["policy"], a["eta"], a["guest_ram_mb"]): a for a in agg}
    assert set(keys) == {("lru", 0.4, 3072), ("lru", 0.4, 6144), ("none", 1.0, 3072)}
    a3 = keys[("lru", 0.4, 3072)]
    assert a3["n_runs"] == 3 and a3["seeds"] == "1,2,3" and a3["mem_saving_pct_sd"] is not None
    assert keys[("lru", 0.4, 6144)]["n_runs"] == 1 and keys[("lru", 0.4, 6144)]["mem_saving_pct_sd"] == 0.0
    assert 0 < a3["achieved_eta"] < 1 and abs(a3["mem_saving_pct"] - 100 * (1 - a3["achieved_eta"])) < 0.2
    prow = pareto_rows(rows)
    assert len(prow) == 3 and all("guest_ram_mb" in p and "n_runs" in p for p in prow)
