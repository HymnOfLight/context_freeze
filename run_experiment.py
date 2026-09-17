#!/usr/bin/env python3
"""Run one or more on-device experiments from JSON configs.

    python run_experiment.py configs/emulator_base.json --policy landlord --eta 0.3
    python run_experiment.py configs/emulator_base.json --sweep-eta 0.2 0.3 0.5 --policies none lru landlord hybrid
    python run_experiment.py configs/emulator_base.json --probe          # only print device capabilities

Checkpoint / resume (断点续跑)
    Every step is checkpointed to <out-dir>/<name>.ckpt.  If a run dies (Ctrl+C, emulator
    crash, adb timeout) continue it with either of

    python run_experiment.py configs/emulator_base.json --resume results/landlord_eta0.3.jsonl
    python run_experiment.py configs/emulator_base.json --policy landlord --eta 0.3 --name landlord_eta0.3 --resume

    A finished run (JSONL ends with {"type": "end"}) is skipped instead of overwritten.

Every run writes <out-dir>/<name>.jsonl (+ .log with the console output); summarise with
`python -m cf.analyze results/*.jsonl`.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from cf.adb import Adb
from cf.device import Device
from cf.logging_util import Logger
from cf.policies import POLICIES
from cf.runner import Experiment, PreflightError, ResumeError, load_config, run_from_config


def load_m_fg(path: str) -> tuple[dict, dict]:
    """Per-app m_fg / a_fg (kB) from the 'budget' record of an earlier result file."""
    with open(path, encoding="utf-8") as f:
        for line in f:
            if '"type": "budget"' in line:
                r = json.loads(line)
                return r["m_fg_kb"], r.get("a_fg_kb", {})
    raise SystemExit(f"{path}: no budget record (run did not reach the end of warmup)")


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)   # `| tee run.log` sees lines immediately
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("config")
    ap.add_argument("--serial", "-s", help="adb serial (e.g. emulator-5554)")
    ap.add_argument("--policy", choices=sorted(POLICIES))
    ap.add_argument("--policies", nargs="+", choices=sorted(POLICIES))
    ap.add_argument("--eta", type=float)
    ap.add_argument("--sweep-eta", type=float, nargs="+")
    ap.add_argument("--T", type=int)
    ap.add_argument("--seed", type=int)
    ap.add_argument("--seeds", type=int, nargs="+", help="replicate every (policy, eta) cell with these trace seeds")
    ap.add_argument("--m-fg-from", metavar="JSONL",
                    help="reuse the warmup m_fg (per-app foreground PSS) of an earlier run so all runs share the same "
                         "budget denominator; typically the first run of a matrix")
    ap.add_argument("--no-strict", action="store_true",
                    help="only warn (instead of aborting) when the system freezer is active or the GPU is software-"
                         "rendered - use for the explicit 'Android default' baseline")
    ap.add_argument("--name", help="result file stem (default <policy>_eta<eta>_<timestamp>)")
    ap.add_argument("--out-dir", help="override config out_dir (default results/)")
    ap.add_argument("--resume", nargs="?", const=True, default=False, metavar="JSONL",
                    help="continue an interrupted run: either give the .jsonl path, or repeat the original "
                         "arguments (incl. --name) and pass --resume alone")
    ap.add_argument("--overwrite", action="store_true", help="replace an existing unfinished result of the same name")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)

    base = load_config(args.config)
    if args.out_dir:
        base["out_dir"] = args.out_dir
    if args.no_strict:
        base["strict_preflight"] = False
    if args.m_fg_from:
        base["fixed_m_fg_kb"], base["fixed_a_fg_kb"] = load_m_fg(args.m_fg_from)
        print(f"budget denominator fixed from {args.m_fg_from}: "
              f"sum m_fg = {sum(base['fixed_m_fg_kb'].values()) // 1024} MB over {len(base['fixed_m_fg_kb'])} apps")
    if args.probe:
        dev = Device(Adb(serial=args.serial, verbose=args.verbose))
        print("root:", dev.adb.ensure_root())
        print(json.dumps(dev.caps_dict() if dev.probe() else {}, indent=1))
        for pkg in base.get("apps", []):
            print(f"{pkg:45s} uid={dev.uid_of(pkg)} activity={dev.launcher_activity(pkg)}")
        return 0

    # --resume <path>: everything (policy, eta, T, name) comes from the checkpoint
    if isinstance(args.resume, str):
        path = args.resume
        if not path.endswith(".jsonl"):
            path += ".jsonl"
        base["out_dir"] = os.path.dirname(path) or "."
        base["name"] = os.path.basename(path)[:-len(".jsonl")]
        ck = Experiment.load_checkpoint(path)
        if ck:                      # for the banner only; Experiment._restore uses the checkpoint anyway
            base["policy"], base["eta"] = ck["cfg"]["policy"], ck["cfg"]["eta"]
        base.setdefault("policy", "?"); base.setdefault("eta", "?")
        jobs = [(base["name"], base)]
    else:
        policies = args.policies or [args.policy or base.get("policy", "landlord")]
        etas = args.sweep_eta or [args.eta if args.eta is not None else base.get("eta", 0.3)]
        seeds = args.seeds or [args.seed]
        jobs = []
        for pol in policies:
            for eta in etas:
                for seed in seeds:
                    cfg = json.loads(json.dumps(base))
                    cfg["policy"] = pol
                    cfg["eta"] = eta
                    if args.T:
                        cfg.setdefault("trace", {})["T"] = args.T
                    if seed is not None:
                        cfg.setdefault("trace", {})["seed"] = seed
                    single = len(policies) * len(etas) * len(seeds) == 1
                    if args.name and single:
                        cfg["name"] = args.name
                    elif args.name and args.seeds:
                        cfg["name"] = f"{args.name}_s{seed}"
                    else:
                        cfg["name"] = f"{pol}_eta{eta}" + (f"_s{seed}" if args.seeds else "") + \
                            f"_{time.strftime('%Y%m%d-%H%M%S')}"
                    jobs.append((cfg["name"], cfg))

    outputs, failed = [], []
    for i, (name, cfg) in enumerate(jobs):
        path = os.path.join(cfg["out_dir"], name + ".jsonl")
        st = Experiment.jsonl_status(path)
        resume = bool(args.resume)
        log = Logger()
        log.head(f"===== [{i + 1}/{len(jobs)}] policy={cfg['policy']} eta={cfg['eta']} -> {path} =====")
        if st["exists"] and st["finished"]:
            log.ok(f"already finished ({st['steps']} steps) - skipping (delete the file to redo)")
            outputs.append(path)
            continue
        if st["exists"] and not resume and not args.overwrite:
            if Experiment.load_checkpoint(path) is not None:
                log.warn(f"unfinished run found ({st['steps']}/{st['T']} steps) - resuming it. "
                         f"Use --overwrite to start from scratch.")
                resume = True
            else:
                log.warn("result file exists without a checkpoint - overwriting")
        if resume and Experiment.load_checkpoint(path) is None:
            if st["exists"]:
                log.err(f"{path} has no .ckpt (finished, or produced before checkpointing existed) - cannot resume")
                failed.append(path)
                continue
            log.warn("nothing to resume yet - starting a fresh run")
            resume = False
        try:
            outputs.append(run_from_config(cfg, serial=args.serial, verbose=args.verbose, resume=resume, log=log))
        except KeyboardInterrupt:
            print(f"\nstopped. Continue with:\n  python3 run_experiment.py {args.config} --resume {path}")
            return 130
        except ResumeError as e:
            log.err(str(e))
            failed.append(path)
        except PreflightError as e:
            log.err(f"{e} - stopping the sweep (every following run would hit the same problem)")
            return 2
        except Exception as e:      # noqa: BLE001 - keep the sweep going, report at the end
            log.err(f"run failed: {type(e).__name__}: {str(e)[:300]}")
            log.warn(f"continue this run later with: python3 run_experiment.py {args.config} --resume {path}")
            failed.append(path)
            if args.verbose:
                raise
        time.sleep(3)

    print("\nresults:")
    for o in outputs:
        print("  ", o)
    for f in failed:
        print("   FAILED/INCOMPLETE", f)
    if outputs:
        print("summarise with: python3 -m cf.analyze " + " ".join(outputs))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
