"""Checkpoint / resume: a run that dies mid-way must be continued from the next step,
with the same trace, the same policy state and no duplicated or missing steps."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from fake_device import FakeDevice  # noqa: E402

from cf.adb import AdbError  # noqa: E402
from cf.analyze import load, summarize  # noqa: E402
from cf.runner import Experiment, ResumeError  # noqa: E402

APPS = [f"com.example.app{i}" for i in range(6)]
T = 14


def cfg_for(tmp_path, policy="landlord"):
    return {"apps": APPS, "policy": policy, "eta": 0.3,
            "trace": {"kind": "zipf", "T": T, "seed": 5}, "dwell_s": 0, "settle_s": 0,
            "warmup_dwell_s": 0, "zram_mb": 512, "out_dir": str(tmp_path), "name": f"{policy}_resume"}


def records(path):
    return [json.loads(l) for l in open(path) if l.strip()]


@pytest.mark.parametrize("policy", ["landlord", "hybrid", "lru"])
def test_crash_then_resume_completes_run(tmp_path, policy):
    cfg = cfg_for(tmp_path, policy)
    dev = FakeDevice(APPS, seed=1)
    dev.fail_at_launch = len(APPS) + 6          # warmup launches + 5 successful steps, crash on the 6th
    exp = Experiment(cfg, dev, log=lambda *a: None)
    with pytest.raises(AdbError):
        exp.run()
    path = os.path.join(str(tmp_path), cfg["name"] + ".jsonl")
    recs = records(path)
    steps = [r["t"] for r in recs if r["type"] == "step"]
    assert steps == list(range(5))
    assert recs[-1]["type"] == "interrupted" and recs[-1]["after_step"] == 4
    assert os.path.exists(path[:-6] + ".ckpt")
    st = Experiment.jsonl_status(path)
    assert st == {"exists": True, "finished": False, "steps": 5, "T": T}

    # the device may have been rebooted: fresh FakeDevice, some processes gone
    dev2 = FakeDevice(APPS, seed=2)
    exp2 = Experiment({"apps": APPS, "out_dir": str(tmp_path), "name": cfg["name"]}, dev2,
                      log=lambda *a: None, resume=True)
    out = exp2.run()
    assert out == path
    recs = records(path)
    types = [r["type"] for r in recs]
    assert types.count("meta") == 1 and types.count("budget") == 1 and types.count("trace") == 1
    assert "resume" in types and types[-1] == "end"
    steps = [r for r in recs if r["type"] == "step"]
    assert [s["t"] for s in steps] == list(range(T))
    trace = next(r for r in recs if r["type"] == "trace")["trace"]
    assert [s["req"] for s in steps] == trace
    assert not os.path.exists(path[:-6] + ".ckpt")     # removed once finished
    # policy state survived: landlord/hybrid keep credits, lru keeps recency
    assert exp2.pol is not None and exp2.t_done == T - 1
    run = load(path)
    row = summarize(run)
    assert row["T"] == T and row["resumes"] == 1 and row["finished"] is True
    assert row["n_hot"] + row["n_warm"] + row["n_cold"] == T


def test_resume_without_checkpoint_raises(tmp_path):
    exp = Experiment({"apps": APPS, "out_dir": str(tmp_path), "name": "nothing"}, FakeDevice(APPS),
                     log=lambda *a: None, resume=True)
    with pytest.raises(ResumeError):
        exp.run()


def test_finished_run_has_no_checkpoint_and_is_detected(tmp_path):
    cfg = cfg_for(tmp_path, "lru")
    path = Experiment(cfg, FakeDevice(APPS), log=lambda *a: None).run()
    st = Experiment.jsonl_status(path)
    assert st["finished"] and st["steps"] == T
    assert Experiment.load_checkpoint(path) is None
    assert os.path.exists(path[:-6] + ".log") is False   # no console log when a custom sink is used


def test_counter_reset_across_resume_does_not_go_negative():
    from cf.analyze import _delta
    steps = [{"after_dwell": {"system": {"vmstat": {"pswpout": v}}}} for v in (100, 250, 400, 30, 90)]
    assert _delta(steps, "pswpout") == 300 + 60
