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
    meta, budget, steps, warm, resumes, finished = None, None, {}, [], 0, False
    with open(path) as f:
        for line in f:
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue          # torn last line after a crash; the step was re-done after --resume
            t = r.get("type")
            if t == "meta":
                meta = r
            elif t == "budget":
                budget = r
            elif t == "warmup":
                warm.append(r)
            elif t == "step":
                steps[r["t"]] = r    # a step re-executed after a resume replaces the torn one
            elif t == "resume":
                resumes += 1
            elif t == "end":
                finished = True
    return {"path": path, "meta": meta or {}, "budget": budget or {},
            "steps": [steps[k] for k in sorted(steps)], "warmup": warm,
            "resumes": resumes, "finished": finished}


def _delta(steps: list[dict], key: str, sub: str = "vmstat") -> int | None:
    """Cumulative growth of a monotone kernel counter. Summed pairwise so that a counter reset
    (emulator rebooted between an interruption and --resume) is skipped instead of going negative."""
    vals = [s["after_dwell"]["system"].get(sub, {}).get(key) for s in steps]
    vals = [v for v in vals if v is not None]
    if len(vals) < 2:
        return None
    return sum(b - a for a, b in zip(vals, vals[1:]) if b >= a)


def kill_reasons(steps: list[dict]) -> str:
    """'bg anr:5,FREEZER/Sync transaction while frozen:3,?:2' from the per-step exit-info lookups."""
    c: dict[str, int] = {}
    for s in steps:
        for pkg in s.get("killed_since_prev", []):
            info = (s.get("kill_info") or {}).get(pkg) or {}
            key = info.get("description") or info.get("reason") or "?"
            c[key] = c.get(key, 0) + 1
    return ",".join(f"{k}:{v}" for k, v in sorted(c.items(), key=lambda kv: -kv[1]))


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
        "resumes": run.get("resumes", 0),
        "finished": run.get("finished", True),
        "seed": cfg.get("trace", {}).get("seed"),
        "guest_ram_mb": (caps.get("mem_total_kb") or 0) // 1024 or None,
        "m_fg_source": run["budget"].get("m_fg_source", "measured"),
        "kill_reasons": kill_reasons(steps),
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


AGG_MAIN_COLS = ["policy", "eta", "guest_ram_mb", "n_runs", "mem_saving_pct", "mem_saving_pct_sd", "achieved_eta",
                 "lat_p50_ms", "lat_p95_ms", "lat_p95_ms_sd", "swap_write_mb", "swap_write_mb_sd", "n_cold", "n_killed",
                 "budget_violations"]

MAIN_COLS = ["policy", "eta", "seed", "guest_ram_mb", "T", "budget_mb", "bg_pss_avg_mb", "bg_pss_peak_mb", "zram_phys_avg_mb",
             "swap_write_mb", "swap_read_mb", "refault_anon", "pgmajfault", "lat_p50_ms", "lat_p95_ms",
             "lat_p99_ms", "n_cold", "n_killed", "budget_violations", "decision_p99_ms", "action_p95_ms"]


GROUP_KEYS = ("policy", "eta", "guest_ram_mb")

# columns averaged over replicates (same policy / eta / guest RAM, different seeds or repeats)
AGG_COLS = ["mem_saving_pct", "achieved_eta", "bg_pss_avg_mb", "bg_pss_peak_mb", "zram_phys_avg_mb",
            "swap_write_mb", "swap_read_mb", "refault_anon", "refault_file", "pgmajfault",
            "lat_p50_ms", "lat_p95_ms", "lat_p99_ms", "lat_resident_p50_ms", "lat_compressed_p50_ms",
            "n_hot", "n_warm", "n_cold", "n_killed", "budget_violations", "decision_p99_ms", "action_p95_ms"]


def _mean_std(vals: list[float]) -> tuple[float | None, float | None]:
    vals = [v for v in vals if v is not None]
    if not vals:
        return None, None
    m = sum(vals) / len(vals)
    sd = (sum((v - m) ** 2 for v in vals) / (len(vals) - 1)) ** 0.5 if len(vals) > 1 else 0.0
    return m, sd


def aggregate(rows: list[dict]) -> list[dict]:
    """Collapse replicate runs into one row per (policy, eta, guest RAM): mean and sample std.

    Runs of the same cell from different guest-RAM configurations are *not* merged - they are
    different experiments and must not be connected in one curve (this is what made the first
    Pareto plot unreadable)."""
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        if r.get("sum_m_fg_mb") and r.get("bg_pss_avg_mb") is not None:
            r = dict(r)
            r["mem_saving_pct"] = 100 * (1 - r["bg_pss_avg_mb"] / r["sum_m_fg_mb"])
            r["achieved_eta"] = r["bg_pss_avg_mb"] / r["sum_m_fg_mb"]
            groups.setdefault(tuple(r.get(k) for k in GROUP_KEYS), []).append(r)
    out = []
    for key, rs in sorted(groups.items(), key=lambda kv: (str(kv[0][0]), kv[0][1] or 0, kv[0][2] or 0)):
        a = dict(zip(GROUP_KEYS, key))
        a["n_runs"] = len(rs)
        a["seeds"] = ",".join(str(r.get("seed")) for r in rs)
        a["T"] = rs[0].get("T")
        a["k"] = rs[0].get("k")
        a["budget_mb"] = _mean_std([r.get("budget_mb") for r in rs])[0]
        a["sum_m_fg_mb"] = _mean_std([r.get("sum_m_fg_mb") for r in rs])[0]
        for c in AGG_COLS:
            m, sd = _mean_std([r.get(c) for r in rs])
            a[c] = None if m is None else round(m, 3 if c == "achieved_eta" else 1)
            a[c + "_sd"] = None if sd is None else round(sd, 3 if c == "achieved_eta" else 1)
        a["finished_all"] = all(r.get("finished", True) for r in rs)
        out.append(a)
    return out


def pareto_rows(rows: list[dict]) -> list[dict]:
    """Memory saving vs latency vs flash writes: one point per (policy, eta, guest RAM), replicates averaged."""
    cols = ["policy", "eta", "guest_ram_mb", "n_runs", "mem_saving_pct", "mem_saving_pct_sd", "achieved_eta",
            "lat_p95_ms", "lat_p95_ms_sd", "lat_p50_ms", "swap_write_mb", "swap_write_mb_sd", "n_cold", "n_killed",
            "budget_violations"]
    return [{c: a.get(c) for c in cols} for a in aggregate(rows)]


def plot_pareto(prow: list[dict], path: str, x: str = "saving") -> None:
    """Two panels: (x) background-memory saving [or achieved eta] vs P95 resume latency / cumulative swap writes.

    * replicates are drawn as mean +/- 1 sd error bars, and only the means are connected (per eta order);
    * `none` ignores eta, so it is drawn as a horizontal reference band (mean +/- sd over all its runs), not a curve;
    * points from different guest-RAM configurations get different markers and are never connected."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed; skipping plot (pip install matplotlib)")
        return
    if x == "eta":
        xkey, xsd, xlabel, invert = "achieved_eta", "achieved_eta_sd", "achieved η = background PSS / Σ m̄ᶠᵍ", True
    else:
        xkey, xsd, xlabel, invert = "mem_saving_pct", "mem_saving_pct_sd", "background memory saving (%)", False
    panels = [("lat_p95_ms", "lat_p95_ms_sd", "resume latency P95 (ms)"),
              ("swap_write_mb", "swap_write_mb_sd", "cumulative swap writes (MB)")]
    fig, axes = plt.subplots(1, 2, figsize=(11.5, 4.4))
    rams = sorted({r["guest_ram_mb"] or 0 for r in prow})
    markers = ["o", "s", "^", "D", "v"]
    pols = sorted({r["policy"] for r in prow if r["policy"] != "none"})
    colors = {p: f"C{i}" for i, p in enumerate(pols)}
    for ax, (ykey, ysd, ylabel) in zip(axes, panels):
        # baseline band
        base = [r for r in prow if r["policy"] == "none" and r.get(ykey) is not None]
        if base:
            ys = [r[ykey] for r in base]
            xs = [r[xkey] for r in base if r.get(xkey) is not None]
            ym, ysd_v = _mean_std(ys)
            # `base` rows are already aggregates: take the within-group sd when there is a single one
            ysd_v = max(ysd_v or 0, max((r.get(ysd) or 0) for r in base))
            ax.axhspan(ym - (ysd_v or 0), ym + (ysd_v or 0), color="0.6", alpha=.25, lw=0,
                       label=f"none (no controller), n={sum(r['n_runs'] for r in base)}: mean ± sd band")
            ax.axhline(ym, color="0.5", lw=1, ls="--")
            if xs:
                xm, xsd_v = _mean_std(xs)
                xsd_v = max(xsd_v or 0, max((r.get(xsd) or 0) for r in base))
                ax.errorbar([xm], [ym], xerr=[xsd_v], yerr=[ysd_v], fmt="x", color="0.35", capsize=3)
        for ri, ram in enumerate(rams):
            for p in pols:
                pts = sorted((r for r in prow if r["policy"] == p and (r["guest_ram_mb"] or 0) == ram
                              and r.get(xkey) is not None and r.get(ykey) is not None),
                             key=lambda r: r["eta"] or 0)
                if not pts:
                    continue
                label = p if len(rams) == 1 else f"{p} ({ram // 1024} GB guest)"
                n = {r["n_runs"] for r in pts}
                label += f" (n={min(n)}" + ("" if len(n) == 1 else f"–{max(n)}") + " seeds)"
                ax.errorbar([r[xkey] for r in pts], [r[ykey] for r in pts],
                            xerr=[r.get(xsd) or 0 for r in pts], yerr=[r.get(ysd) or 0 for r in pts],
                            fmt=markers[ri % len(markers)] + ("-" if len(pts) > 1 else ""),
                            color=colors[p], capsize=3, ms=5, lw=1.2, label=label)
                if ax is axes[0]:
                    for r in pts:
                        ax.annotate(f"η={r['eta']}", (r[xkey], r[ykey]), fontsize=7, xytext=(4, 4),
                                    textcoords="offset points")
        ax.set_xlabel(xlabel); ax.set_ylabel(ylabel); ax.grid(alpha=.3)
        if invert:
            ax.invert_xaxis()
    axes[0].legend(fontsize=7, loc="best")
    title = "memory saving – resume latency – swap writes   (points: mean over seeds, bars: ± 1 sd)"
    if len(rams) > 1:
        title += "\nmarkers = guest RAM; series from different RAM configurations are never connected"
    fig.suptitle(title, fontsize=10)
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
    ap.add_argument("--x", choices=["saving", "eta"], default="saving",
                    help="Pareto x-axis: memory saving %% (default) or achieved eta")
    ap.add_argument("--agg", help="write the replicate-aggregated table (mean/sd per policy, eta, guest RAM) as CSV")
    args = ap.parse_args(argv)
    rows = [summarize(load(f)) for f in args.files]
    rows.sort(key=lambda r: (str(r["policy"]), r["eta"] or 0, r.get("guest_ram_mb") or 0, r.get("seed") or 0))
    print_table(rows, None if args.all_cols else MAIN_COLS)
    agg = aggregate(rows)
    if any(a["n_runs"] > 1 for a in agg) or len({a["guest_ram_mb"] for a in agg}) > 1:
        print("\naggregated over replicates (mean ± sd):")
        print_table(agg, AGG_MAIN_COLS)
    if args.agg:
        with open(args.agg, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(agg[0].keys()))
            w.writeheader(); w.writerows(agg)
        print(f"wrote {args.agg}")
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
            plot_pareto(prow, args.plot, x=args.x)
    return 0


if __name__ == "__main__":
    sys.exit(main())
