#!/usr/bin/env python3
"""Run one or more on-device experiments from JSON configs.

    python run_experiment.py configs/emulator_base.json --policy landlord --eta 0.3
    python run_experiment.py configs/emulator_base.json --sweep-eta 0.2 0.3 0.5 --policies none lru landlord hybrid
    python run_experiment.py configs/emulator_base.json --probe          # only print device capabilities

Every run writes results/<name>.jsonl; summarise with `python -m cf.analyze results/*.jsonl`.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from cf.adb import Adb
from cf.device import Device
from cf.policies import POLICIES
from cf.runner import load_config, run_from_config


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("config")
    ap.add_argument("--serial", "-s", help="adb serial (e.g. emulator-5554)")
    ap.add_argument("--policy", choices=sorted(POLICIES))
    ap.add_argument("--policies", nargs="+", choices=sorted(POLICIES))
    ap.add_argument("--eta", type=float)
    ap.add_argument("--sweep-eta", type=float, nargs="+")
    ap.add_argument("--T", type=int)
    ap.add_argument("--seed", type=int)
    ap.add_argument("--name")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)

    base = load_config(args.config)
    if args.probe:
        dev = Device(Adb(serial=args.serial, verbose=args.verbose))
        print("root:", dev.adb.ensure_root())
        print(json.dumps(dev.caps_dict() if dev.probe() else {}, indent=1))
        for pkg in base.get("apps", []):
            print(f"{pkg:45s} uid={dev.uid_of(pkg)} activity={dev.launcher_activity(pkg)}")
        return 0

    policies = args.policies or [args.policy or base.get("policy", "landlord")]
    etas = args.sweep_eta or [args.eta if args.eta is not None else base.get("eta", 0.3)]
    outputs = []
    for pol in policies:
        for eta in etas:
            cfg = json.loads(json.dumps(base))
            cfg["policy"] = pol
            cfg["eta"] = eta
            if args.T:
                cfg.setdefault("trace", {})["T"] = args.T
            if args.seed is not None:
                cfg.setdefault("trace", {})["seed"] = args.seed
            cfg["name"] = args.name if (args.name and len(policies) * len(etas) == 1) else \
                f"{pol}_eta{eta}_{time.strftime('%Y%m%d-%H%M%S')}"
            print(f"\n===== policy={pol} eta={eta} =====")
            outputs.append(run_from_config(cfg, serial=args.serial, verbose=args.verbose))
            time.sleep(3)
    print("\nresults:")
    for o in outputs:
        print(" ", o)
    print("summarise with: python -m cf.analyze " + " ".join(outputs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
