"""Summarise runner JSONL logs into the metrics listed in the experiment plan:

  平均及峰值后台 PSS, ZRAM 逻辑量/物理量, 累计 swap 写入/读取, major fault 与 refault,
  恢复时延 P50/P95/P99 (按 HOT/WARM/COLD 拆分), 预算违约次数, 被杀次数, 控制器开销.

    python -m cf.analyze results/*.jsonl [--csv out.csv] [--pareto pareto.csv] [--plot pareto.png]
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from typing import Iterable


def pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, int(round(p * (len(v) - 1))))]


def load(path: str) -> dict:
    meta, budget, steps, warm = None, None, [], []
    with open(path) as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            t = r.get("type")
            if t == "meta":
                meta = r
            elif t == "budget":
                budget = r
            elif t == "warmup":
                warm.append(r)
            elif t == "step":
                steps.append(r)
    return {"path": path, "meta": meta or {}, "budget": budget or {}, "steps": steps, "warmup": warm}


def _delta(steps: list[dict], key: str, sub: str = "vmstat") -> int | None:
    vals = [s["after_dwell"]["system"].get(sub, {}).get(key) for s in steps]
    vals = [v for v in vals if v is not None]
    if len(vals) < 2:
        return None
    return vals[-1] - vals[0]


def summarize(run: dict) -> dict:
    cfg = run["meta"].get("config", {})
    caps = run["meta"].get("caps", {})
    page = caps.get("page_size", 4096)
    steps = run["steps"]
    apps = run["meta"].get("apps", [])
    B_kb = run["budget"].get("budget_kb")

    bg_pss, bg_anon, zram_logical, zram_phys, mem_avail = [], [], [], [], []
    for s in steps:
        req = s["req"]
        a = s["after_dwell"]["apps"]
        bg = [a[p] for p in apps if p != req and p in a]
        bg_pss.append(sum(x["pss_kb"] for x in bg) / 1024)
        bg_anon.append(sum(x["pss_anon_kb"] for x in bg) / 1024)
        zram_logical.append(sum(x["swap_pss_kb"] for x in bg) / 1024)
        z = s["after_dwell"]["system"].get("zram") or {}
        zram_phys.append(z.get("mem_used_total", 0) / 2 ** 20)
        mem_avail.append(s["after_dwell"]["system"].get("meminfo", {}).get("MemAvailable", 0) / 1024)

    lat_all = [s["launch"].get("total_time_ms") for s in steps if s["launch"].get("total_time_ms")]
    by_state: dict[str, list[int]] = {}
    for s in steps:
        st = s["launch"].get("launch_state") or "UNKNOWN"
        if s["launch"].get("total_time_ms"):
            by_state.setdefault(st, []).append(s["launch"]["total_time_ms"])
    lat_comp = [s["launch"]["total_time_ms"] for s in steps if s.get("was_compressed") and s["launch"].get("total_time_ms")]
    lat_res = [s["launch"]["total_time_ms"] for s in steps if not s.get("was_compressed") and s["launch"].get("total_time_ms")]

    pswpout = _delta(steps, "pswpout")
    pswpin = _delta(steps, "pswpin")
    refault = _delta(steps, "workingset_refault_anon")
    refault_file = _delta(steps, "workingset_refault_file")
    if refault is None and refault_file is None:
        refault = _delta(steps, "workingset_refault")
    majflt = _delta(steps, "pgmajfault")
    psi_full = _delta(steps, "full_total", "psi")
    psi_some = _delta(steps, "some_total", "psi")

    T = len(steps)
    row = {
        "file": os.path.basename(run["path"]),
        "policy": cfg.get("policy"),
        "eta": cfg.get("eta"),
        "T": T,
        "k": len(apps),
        "budget_mb": None if B_kb is None else round(B_kb / 1024, 1),
        "sum_m_fg_mb": round(run["budget"].get("sum_m_fg_kb", 0) / 1024, 1),
        "bg_pss_avg_mb": round(sum(bg_pss) / T, 1) if T else None,
        "bg_pss_peak_mb": round(max(bg_pss), 1) if bg_pss else None,
        "bg_anon_avg_mb": round(sum(bg_anon) / T, 1) if T else None,
        "zram_logical_avg_mb": round(sum(zram_logical) / T, 1) if T else None,
        "zram_phys_avg_mb": round(sum(zram_phys) / T, 1) if T else None,
        "zram_phys_peak_mb": round(max(zram_phys), 1) if zram_phys else None,
        "mem_avail_avg_mb": round(sum(mem_avail) / T, 1) if T else None,
        "swap_write_mb": None if pswpout is None else round(pswpout * page / 2 ** 20, 1),
        "swap_read_mb": None if pswpin is None else round(pswpin * page / 2 ** 20, 1),
        "refault_anon": refault,
        "refault_file": refault_file,
        "pgmajfault": majflt,
        "psi_full_ms": None if psi_full is None else round(psi_full / 1000, 1),
        "psi_some_ms": None if psi_some is None else round(psi_some / 1000, 1),
        "lat_p50_ms": pct(lat_all, 0.5), "lat_p95_ms": pct(lat_all, 0.95), "lat_p99_ms": pct(lat_all, 0.99),
        "lat_resident_p50_ms": pct(lat_res, 0.5), "lat_compressed_p50_ms": pct(lat_comp, 0.5),
        "lat_compressed_p95_ms": pct(lat_comp, 0.95),
        "n_hot": len(by_state.get("HOT", [])), "n_warm": len(by_state.get("WARM", [])),
        "n_cold": len(by_state.get("COLD", [])),
        "n_timeout": len(by_state.get("TIMEOUT", [])),
        "n_front": sum(1 for s in steps if s["launch"].get("launch_state") == "FRONT"),
        "n_killed": sum(len(s.get("killed_since_prev", [])) for s in steps),
        "budget_violations": sum(1 for s in steps if s.get("budget_violation")),
        "n_compress_actions": sum(len(s["actions"].get("reclaim", {})) for s in steps),
        "decision_p50_ms": pct([s["decision_ms"] for s in steps], 0.5),
        "decision_p99_ms": pct([s["decision_ms"] for s in steps], 0.99),
        "action_p50_ms": pct([s["action_ms"] for s in steps], 0.5),
        "action_p95_ms": pct([s["action_ms"] for s in steps], 0.95),
        "reclaim_methods": ",".join(sorted({h for s in steps for h in s["actions"].get("reclaim", {}).values()})),
        "freezer": caps.get("freezer"),
    }
    if row["sum_m_fg_mb"] and row["bg_pss_avg_mb"] is not None:
        row["bg_pss_avg_over_sum_fg"] = round(row["bg_pss_avg_mb"] / row["sum_m_fg_mb"], 3)
    return row


def print_table(rows: list[dict], cols: Iterable[str] | None = None) -> None:
    cols = list(cols) if cols else [c for c in rows[0] if c not in ("file",)]
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    print("  ".join(c.ljust(widths[c]) for c in cols))
    for r in rows:
        print("  ".join(str(r.get(c, "")).ljust(widths[c]) for c in cols))


MAIN_COLS = ["policy", "eta", "T", "budget_mb", "bg_pss_avg_mb", "bg_pss_peak_mb", "zram_phys_avg_mb",
             "swap_write_mb", "swap_read_mb", "refault_anon", "pgmajfault", "lat_p50_ms", "lat_p95_ms",
             "lat_p99_ms", "n_cold", "n_killed", "budget_violations", "decision_p99_ms", "action_p95_ms"]


def pareto_rows(rows: list[dict]) -> list[dict]:
    """Memory saving vs latency vs flash writes, one point per (policy, eta)."""
    out = []
    for r in rows:
        if r.get("sum_m_fg_mb") and r.get("bg_pss_avg_mb") is not None:
            out.append({"policy": r["policy"], "eta": r["eta"],
                        "mem_saving_pct": round(100 * (1 - r["bg_pss_avg_mb"] / r["sum_m_fg_mb"]), 1),
                        "lat_p95_ms": r["lat_p95_ms"], "swap_write_mb": r["swap_write_mb"],
                        "n_cold": r["n_cold"]})
    return out


def plot_pareto(prow: list[dict], path: str) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed; skipping plot (pip install matplotlib)")
        return
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2))
    pols = sorted({r["policy"] for r in prow})
    for p in pols:
        pts = sorted((r for r in prow if r["policy"] == p), key=lambda r: r["eta"] or 0)
        axes[0].plot([r["mem_saving_pct"] for r in pts], [r["lat_p95_ms"] for r in pts], "o-", label=p)
        axes[1].plot([r["mem_saving_pct"] for r in pts], [r["swap_write_mb"] or 0 for r in pts], "o-", label=p)
        for r in pts:
            axes[0].annotate(f"η={r['eta']}", (r["mem_saving_pct"], r["lat_p95_ms"]), fontsize=7)
    axes[0].set_xlabel("background memory saving (%)"); axes[0].set_ylabel("resume latency P95 (ms)")
    axes[1].set_xlabel("background memory saving (%)"); axes[1].set_ylabel("cumulative swap writes (MB)")
    axes[0].legend(fontsize=8); axes[0].grid(alpha=.3); axes[1].grid(alpha=.3)
    fig.suptitle("memory saving – resume latency – swap writes")
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    print(f"wrote {path}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+")
    ap.add_argument("--csv")
    ap.add_argument("--pareto", help="write Pareto points CSV")
    ap.add_argument("--plot", help="write Pareto PNG (needs matplotlib)")
    ap.add_argument("--all-cols", action="store_true")
    args = ap.parse_args(argv)
    rows = [summarize(load(f)) for f in args.files]
    rows.sort(key=lambda r: (str(r["policy"]), r["eta"] or 0))
    print_table(rows, None if args.all_cols else MAIN_COLS)
    if args.csv:
        with open(args.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader(); w.writerows(rows)
        print(f"wrote {args.csv}")
    if args.pareto or args.plot:
        prow = pareto_rows(rows)
        if args.pareto:
            with open(args.pareto, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(prow[0].keys()))
                w.writeheader(); w.writerows(prow)
            print(f"wrote {args.pareto}")
        if args.plot:
            plot_pareto(prow, args.plot)
    return 0


if __name__ == "__main__":
    sys.exit(main())
