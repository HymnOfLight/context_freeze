"""Markdown report for a results directory: the summary tables cf.linux.analyze prints, as Markdown,
plus links to the figures.  Written by `python3 -m cf.linux.analyze ... --md report.md` and by
scripts/ubuntu/run_all.sh at the end of a matrix."""
from __future__ import annotations

import os
import time

from .analyze import MAIN_COLS, PER_APP_COLS, PER_PROC_COLS, SQUEEZE_APP_COLS, AGG_COLS_LINUX


def _fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.3g}" if abs(v) < 1000 else f"{v:.0f}"
    return str(v)


def md_table(rows: list[dict], cols: list[str]) -> str:
    if not rows:
        return "_(no data)_\n"
    cols = [c for c in cols if any(c in r for r in rows)]
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        lines.append("| " + " | ".join(_fmt(r.get(c)) for c in cols) + " |")
    return "\n".join(lines) + "\n"


def write_markdown(path: str, rows: list[dict], agg: list[dict], per_app: list[dict], segments: list[dict],
                   plots_dir: str | None = None, per_proc: list[dict] | None = None,
                   main_cols: list[str] | None = None, squeeze_apps: list[dict] | None = None) -> None:
    n_lost = sum(r.get("state_lost_events") or 0 for r in rows)
    n_oom = sum(r.get("oom_kills") or 0 for r in rows)
    n_cold = sum(r.get("n_cold") or 0 for r in rows)
    n_wchk = sum(r.get("work_checks") or 0 for r in rows)
    n_wfail = sum(r.get("work_fail") or 0 for r in rows)
    unfinished = [r for r in rows if not r.get("finished", True)]
    out = ["# Ubuntu throttling results\n", f"_generated {time.strftime('%Y-%m-%d %H:%M')}; {len(rows)} run(s)_\n",
           "## Invariants (must be 0)\n",
           f"- processes / windows lost or OOM-killed while throttled (`state_lost_events`): **{n_lost}**\n"
           f"- OOM kills (`oom_kills`): **{n_oom}**\n"
           f"- COLD switches = an app had to be restarted (`n_cold`): **{n_cold}**\n"
           + (f"- scripted-work state checks after a background phase (`work_checks`): {n_wchk}, "
              f"content lost (`work_fail`): **{n_wfail}**\n" if n_wchk else "")
           + (f"- unfinished runs: {', '.join(str(r.get('file')) for r in unfinished)}\n" if unfinished else ""),
           "\n## Per run\n", md_table(rows, main_cols or MAIN_COLS)]
    if per_proc is not None:
        out.append("\n> **per-process accounting** (`per_process`): `fg_pss_avg_mb` is the PSS of the requested app at "
                   "the end of its own dwell, `bg_pss_quiet_avg_mb` the summed PSS of the other apps at the end of a "
                   "dwell, `bg_squeeze_pct` how far below their last foreground PSS they were pushed, and the "
                   "`quiet_*_per_min` columns are rates over the quiet part of the dwells (after `quiet_after_s`): "
                   "system swap-out (`pswpout`), pages reclaimed from the background cgroups (`pgsteal`), "
                   "and growth of their swap.\n")
    infeasible = [r for r in rows if r.get("eta_feasible") is False]
    if infeasible:
        out.append("\n> **eta below the shrunk floor** in "
                   + ", ".join(str(r.get("file")) for r in infeasible)
                   + ": shrunk apps keep `memory.high = mem_frac × m_fg` (≥ `mem_min_mb`), so the background footprint "
                   "cannot go under `eta_min × Σm_fg`; these cells keep nothing resident and are equivalent to `static`. "
                   "Compare policies at `eta ≥ eta_min` or lower `throttle.mem_frac`.\n")
    if any(a.get("n_runs", 1) > 1 for a in agg):
        out += ["\n## Aggregated over replicates (mean ± sd)\n", md_table(agg, AGG_COLS_LINUX)]
    if segments:
        out += ["\n## Day traces: per-scenario segments\n", md_table(segments, list(segments[0].keys()))]
    if per_app:
        out += ["\n## Per app\n", md_table(per_app, PER_APP_COLS)]
    if squeeze_apps:
        out += ["\n## Per app: active footprint, background resident, CPU, swap traffic, switch-back\n",
                md_table(squeeze_apps, SQUEEZE_APP_COLS),
                "\n_m_fg = warmup PSS+Swap (active footprint); bg_resid = resident PSS at the end of the dwells spent "
                "in the background; bg_cpu_pct_core = CPU it got there as % of one core (cap = 100 x cpu_cores); "
                "bg_reclaim = pages the kernel took from its cgroup per minute (pgsteal), bg_swapin = major faults "
                "x 4 KB per minute; lat = switch-back time when it was requested (n_req)._\n"]
    if per_proc:
        out += ["\n## Per process (foreground vs background PSS)\n", md_table(per_proc, PER_PROC_COLS)]
        miss = [r for r in per_proc if (r.get("pss_fg_mb") or 0) >= 20 and r.get("squeeze_pct") is not None
                and r["squeeze_pct"] < 90 and not str(r.get("run", "")).startswith("none")]
        if miss:
            out.append("\n> processes still above **10 % of their foreground PSS** in the background "
                       "(`squeeze_pct` < 90, fg >= 20 MB): "
                       + ", ".join(f"{r['run']}/{r['app']}/{r['proc']} {100 - r['squeeze_pct']:.0f} %" for r in miss)
                       + " - what is left is what `memory.reclaim` cannot take (pinned, GPU / shared buffers, pages "
                       "the app keeps touching); compare `swap_growth_mb_per_min` to see whether it is still fighting.\n")
    if plots_dir and os.path.isdir(plots_dir):
        rel = os.path.relpath(plots_dir, os.path.dirname(os.path.abspath(path)) or ".")
        pngs = sorted(f for f in os.listdir(plots_dir) if f.endswith(".png"))
        if pngs:
            out.append("\n## Figures\n")
            for f in ("squeeze_apps.png", "bg_tradeoff.png", "latency_cdf.png", "pareto.png"):
                if f in pngs:
                    out.append(f"![{f}]({rel}/{f})\n")
            out.append("\nper run:\n")
            for f in pngs:
                if f not in ("squeeze_apps.png", "bg_tradeoff.png", "latency_cdf.png", "pareto.png"):
                    out.append(f"- [{f}]({rel}/{f})\n")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out))
