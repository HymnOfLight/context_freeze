"""Summaries for the Ubuntu throttling runs.

Everything cf.analyze reports for Android (background PSS, swap I/O, refaults, latency percentiles,
budget violations, decision / action overhead) still applies - the step records keep the same
keys - plus the throttling-specific metrics:

  activate_p50_ms / ping_p50/95/99_ms       WM switch time and event-loop responsiveness after the switch
  ping_resident_p50_ms / ping_shrunk_p50_ms responsiveness when the app came back from tier bg vs shrunk
  bg_cores_avg, bg_throttled_pct            CPU the background apps actually got / fraction of time throttled
  download_mb, download_mbps                bytes the download-category apps wrote while in the background
  notif_total, notif_per_h                  notifications delivered by the push-category apps while limited
  compute_cpu_s                             CPU seconds obtained by the compute-category apps in the background
  state_lost_events, oom_kills              must be 0: no process, window or state may be lost
  psi_cpu_some_ms                           CPU pressure felt system-wide (foreground stalls)

    python3 -m cf.linux.analyze results/ubuntu_matrix/*.jsonl --csv summary.csv --agg agg.csv --plot pareto.png
"""
from __future__ import annotations

import argparse
import csv
import sys

from ..analyze import (AGG_MAIN_COLS, aggregate, load, pct, plot_pareto, print_table, segment_rows,
                       summarize as base_summarize, _delta)

EXTRA_AGG_COLS = ["activate_p50_ms", "ping_p50_ms", "ping_p95_ms", "ping_p99_ms", "ping_resident_p50_ms",
                  "ping_shrunk_p50_ms", "bg_cores_avg", "bg_throttled_pct", "download_mb", "download_mbps",
                  "notif_total", "notif_per_h", "compute_cpu_s", "state_lost_events", "oom_kills",
                  "psi_cpu_some_ms", "n_ping_timeout"]

MAIN_COLS = ["policy", "scenario", "eta", "seed", "cpu", "mem_frac", "T", "budget_mb", "bg_pss_avg_mb",
             "bg_pss_peak_mb", "swap_write_mb", "lat_p50_ms", "lat_p95_ms", "ping_p95_ms", "n_cold",
             "bg_cores_avg", "bg_throttled_pct", "download_mbps", "notif_per_h", "compute_cpu_s",
             "state_lost_events", "oom_kills", "budget_violations"]

AGG_COLS_LINUX = AGG_MAIN_COLS + ["ping_p95_ms", "bg_cores_avg", "download_mbps", "notif_per_h",
                                  "compute_cpu_s", "state_lost_events"]


def summarize(run: dict) -> dict:
    row = base_summarize(run)
    meta = run["meta"]
    cfg = meta.get("config", {})
    prof = meta.get("profile") or cfg.get("throttle") or {}
    cats = meta.get("categories") or {}
    steps = run["steps"]

    act = [s["launch"].get("activate_ms") for s in steps if s["launch"].get("activate_ms") is not None]
    ping = [s["launch"]["ping_ms"] for s in steps if s["launch"].get("ping_ms") is not None]
    ping_shr = [s["launch"]["ping_ms"] for s in steps
                if s.get("was_shrunk") and s["launch"].get("ping_ms") is not None]
    ping_res = [s["launch"]["ping_ms"] for s in steps
                if not s.get("was_shrunk") and s["launch"].get("ping_ms") is not None]
    n_ping_to = sum(1 for s in steps if s["launch"].get("status") == "ping-timeout")

    cores, thr_num, thr_den = [], 0.0, 0.0
    dl_mb, dl_wall, ntf, cpu_s = 0.0, 0.0, 0, 0.0
    lost_events, oom = 0, 0
    for s in steps:
        req = s["req"]
        bg = s.get("bg") or {}
        others = {p: b for p, b in bg.items() if p != req}
        if others:
            cores.append(sum(b.get("cores") or 0 for b in others.values()))
        for p, b in others.items():
            wall = b.get("wall_s") or 0
            if b.get("tier") in ("bg", "shrunk"):
                thr_num += b.get("throttled_ms", 0) / 1000
                thr_den += wall
            c = cats.get(p, "")
            if c == "download":
                dl_mb += b.get("write_mb", 0)
                dl_wall += wall
            elif c == "push":
                ntf += b.get("notifications", 0)
            elif c == "compute":
                cpu_s += b.get("cg_cpu_ms", 0) / 1000
        sl = s.get("state_lost") or {}
        lost_events += len(sl)
        oom += sum(v.get("oom_kill", 0) for v in sl.values() if isinstance(v, dict))
    wall_total = sum(s.get("step_s", 0) for s in steps)
    psi_cpu = _delta(steps, "some_total", "psi_cpu")

    cpu_desc = {"quota": f"{prof.get('cpu_cores', 1):g}c", "cpuset": f"set{prof.get('cpuset_cpus')}",
                "both": f"{prof.get('cpu_cores', 1):g}c+set", "none": "free"}.get(prof.get("cpu_mode"), "?")
    row.update({
        "backend": meta.get("backend", "linux"),
        "cpu": cpu_desc, "mem_frac": prof.get("mem_frac"),
        "cpu_throttle_resident": cfg.get("cpu_throttle_resident"),
        "activate_p50_ms": pct(act, 0.5), "activate_p95_ms": pct(act, 0.95),
        "ping_p50_ms": pct(ping, 0.5), "ping_p95_ms": pct(ping, 0.95), "ping_p99_ms": pct(ping, 0.99),
        "ping_resident_p50_ms": pct(ping_res, 0.5), "ping_shrunk_p50_ms": pct(ping_shr, 0.5),
        "ping_shrunk_p95_ms": pct(ping_shr, 0.95), "n_ping_timeout": n_ping_to,
        "bg_cores_avg": round(sum(cores) / len(cores), 3) if cores else None,
        "bg_throttled_pct": round(100 * thr_num / thr_den, 1) if thr_den else None,
        "download_mb": round(dl_mb, 1), "download_mbps": round(dl_mb / dl_wall, 3) if dl_wall else None,
        "notif_total": ntf, "notif_per_h": round(ntf / wall_total * 3600, 1) if wall_total else None,
        "compute_cpu_s": round(cpu_s, 1),
        "state_lost_events": lost_events, "oom_kills": oom,
        "psi_cpu_some_ms": None if psi_cpu is None else round(psi_cpu / 1000, 1),
        "substitutions": ";".join(f"{a}->{b}" for a, b in (meta.get("substitutions") or [])),
        "guest_ram_mb": (meta.get("caps", {}).get("mem_total_kb") or 0) // 1024 or None,
    })
    return row


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+")
    ap.add_argument("--csv")
    ap.add_argument("--agg", help="replicate-aggregated table (mean/sd per policy, scenario, eta) as CSV")
    ap.add_argument("--pareto", help="write Pareto points CSV")
    ap.add_argument("--plot", help="write Pareto PNG (needs matplotlib)")
    ap.add_argument("--x", choices=["saving", "eta"], default="saving")
    ap.add_argument("--all-cols", action="store_true")
    args = ap.parse_args(argv)
    runs = [load(f) for f in args.files]
    rows = [summarize(r) for r in runs]
    rows.sort(key=lambda r: (str(r["policy"]), str(r.get("scenario")), r["eta"] or 0, r.get("seed") or 0))
    print_table(rows, None if args.all_cols else MAIN_COLS)
    seg = [x for r in runs for x in segment_rows(r)]
    if seg:
        print("\nper-scenario segments (day traces):")
        print_table(seg)
    agg = aggregate(rows, EXTRA_AGG_COLS)
    if any(a["n_runs"] > 1 for a in agg):
        print("\naggregated over replicates (mean ± sd):")
        print_table(agg, AGG_COLS_LINUX)
    if args.agg and agg:
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
        cols = ["policy", "scenario", "eta", "guest_ram_mb", "n_runs", "mem_saving_pct", "mem_saving_pct_sd",
                "achieved_eta", "lat_p95_ms", "lat_p95_ms_sd", "lat_p50_ms", "swap_write_mb", "swap_write_mb_sd",
                "n_cold", "n_killed", "budget_violations", "ping_p95_ms", "bg_cores_avg", "download_mbps",
                "notif_per_h", "state_lost_events"]
        prow = [{c: a.get(c) for c in cols} for a in agg]
        if args.pareto and prow:
            with open(args.pareto, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(prow[0].keys()))
                w.writeheader(); w.writerows(prow)
            print(f"wrote {args.pareto}")
        if args.plot and prow:
            plot_pareto(prow, args.plot, x=args.x)
    return 0


if __name__ == "__main__":
    sys.exit(main())
