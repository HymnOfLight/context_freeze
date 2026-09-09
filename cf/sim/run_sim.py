"""Synthetic (Linux-stage) validation: compare online policies with the offline Bellman OPT.

    python -m cf.sim.run_sim --k 8 --T 300 --eta 0.1 0.2 0.3 --trace zipf --seed 1
    python -m cf.sim.run_sim --k 10 --T 200 --trace markov --drift-at 100 --out results/sim.json
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time

from ..policies import make_policy, POLICIES
from ..trace import make_trace
from .bellman import solve_opt
from .model import Lambdas, SimConfig, budget, evaluate_policy, make_apps


def run_one(cfg: SimConfig, trace: list[str], policies: list[str], with_opt: bool = True) -> list[dict]:
    apps = make_apps(cfg)
    names = [a.name for a in apps]
    B = budget(cfg, apps)
    rows = []
    for pname in policies:
        kw = {"trace": trace} if pname == "belady" else {}
        pol = make_policy(pname, names, **kw)
        t0 = time.time()
        res = evaluate_policy(pol, trace, apps, cfg.lam, B)
        row = res.summary()
        row["wall_s"] = round(time.time() - t0, 3)
        rows.append(row)
    if with_opt:
        t0 = time.time()
        opt, _ = solve_opt(trace, apps, cfg.lam, B)
        row = opt.summary()
        row["wall_s"] = round(time.time() - t0, 3)
        rows.append(row)
        for r in rows:
            r["ratio_to_opt"] = round(r["total_cost"] / opt.total_cost, 3) if opt.total_cost else None
    total_m = sum(a.m for a in apps)
    # smallest eta for which "everything compressed" fits: sum_i rho_i a_i (minus the largest
    # foreground exclusion) / sum_i m_i  -- a property of the app mix, not of any policy
    floor = (sum(a.rho * a.a for a in apps) - max(a.rho * a.a for a in apps)) / total_m
    for r in rows:
        r["eta"] = cfg.eta
        r["eta_floor"] = round(floor, 3)
        r["budget_mb"] = round(B / (1024 * 1024), 1)
        r["k"] = cfg.k
        r["T"] = len(trace)
    return rows


def print_table(rows: list[dict]) -> None:
    cols = ["eta", "eta_floor", "policy", "total_cost", "ratio_to_opt", "zin_mb", "zout_mb", "avg_M_mb",
            "peak_M_mb", "lat_p50_ms", "lat_p95_ms", "lat_p99_ms", "zram_resumes",
            "budget_violations", "eta_rank", "wall_s"]
    cols = [c for c in cols if any(c in r for r in rows)]
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    print("  ".join(c.ljust(widths[c]) for c in cols))
    for r in rows:
        print("  ".join(str(r.get(c, "")).ljust(widths[c]) for c in cols))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--k", type=int, default=8, help="number of Top-k apps (<= 12 for OPT)")
    ap.add_argument("--T", type=int, default=200)
    ap.add_argument("--eta", type=float, nargs="+", default=[0.1])
    ap.add_argument("--trace", default="zipf", choices=["zipf", "markov", "round_robin", "replay"])
    ap.add_argument("--trace-path", help="JSONL/text trace for --trace replay")
    ap.add_argument("--zipf-s", type=float, default=1.0)
    ap.add_argument("--stickiness", type=float, default=0.3)
    ap.add_argument("--drift-at", type=int, default=None, help="permute popularity at step t")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--policies", nargs="+", default=["none", "lru", "lfu", "landlord", "markov", "hybrid", "belady"],
                    choices=sorted(POLICIES))
    ap.add_argument("--no-opt", action="store_true", help="skip the Bellman DP")
    ap.add_argument("--lambda-z", type=float, default=1.0)
    ap.add_argument("--lambda-m", type=float, default=0.02)
    ap.add_argument("--lambda-L", type=float, default=1.0)
    ap.add_argument("--L-max", type=float, default=None, help="use phi(L)=[L-Lmax]_+^2 (ms)")
    ap.add_argument("--out", help="write JSON (and .csv) results")
    args = ap.parse_args(argv)

    lam = Lambdas(z=args.lambda_z, m=args.lambda_m, L=args.lambda_L, L_max=args.L_max)
    all_rows: list[dict] = []
    for eta in args.eta:
        cfg = SimConfig(k=args.k, eta=eta, lam=lam, seed=args.seed)
        names = [a.name for a in make_apps(cfg)]
        tcfg = {"kind": args.trace, "T": args.T, "s": args.zipf_s, "stickiness": args.stickiness,
                "seed": args.seed, "drift_at": args.drift_at, "path": args.trace_path}
        trace = make_trace(names, tcfg)
        rows = run_one(cfg, trace, args.policies, with_opt=not args.no_opt)
        all_rows.extend(rows)
    print_table(all_rows)
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w") as f:
            json.dump({"args": vars(args), "rows": all_rows}, f, indent=1)
        csv_path = os.path.splitext(args.out)[0] + ".csv"
        with open(csv_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=sorted({k for r in all_rows for k in r}))
            w.writeheader()
            w.writerows(all_rows)
        print(f"\nwrote {args.out} and {csv_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
