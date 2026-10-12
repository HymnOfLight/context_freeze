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
                  "psi_cpu_some_ms", "n_ping_timeout", "n_resident_avg"]

MAIN_COLS = ["policy", "scenario", "eta", "seed", "cpu", "mem_frac", "T", "budget_mb", "bg_pss_avg_mb",
             "bg_pss_peak_mb", "swap_write_mb", "lat_p50_ms", "lat_p95_ms", "ping_p95_ms", "n_cold",
             "bg_cores_avg", "bg_throttled_pct", "download_mbps", "notif_per_h", "compute_cpu_s",
             "state_lost_events", "oom_kills", "budget_violations", "n_resident_avg", "eta_min"]

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
        "n_mobile": sum(1 for k in (meta.get("kinds") or {}).values() if k == "mobile"),
        "n_placeholders": len(meta.get("placeholders") or []),
        "guest_ram_mb": (meta.get("caps", {}).get("mem_total_kb") or 0) // 1024 or None,
        "eta_min": run["budget"].get("eta_min"),
        "n_resident_avg": round(sum(len([a for a in s.get("resident", []) if a != s["req"]]) for s in steps)
                                / len(steps), 2) if steps else None,
    })
    # a budgeted cell whose budget is below the shrunk floor is identical to static (nothing can be resident)
    em = row["eta_min"]
    row["eta_feasible"] = None if em is None or row["policy"] in ("none", "static") else bool((row["eta"] or 0) >= em)
    return row


PER_APP_COLS = ["run", "app", "kind", "category", "n_req", "n_cold", "lat_bg_p50_ms", "lat_shrunk_p50_ms",
                "ping_shrunk_p50_ms", "m_fg_mb", "pss_bg_avg_mb", "pss_shrunk_avg_mb", "mem_bg_avg_mb",
                "mem_shrunk_avg_mb", "t_fg_s", "t_bg_s",
                "t_shrunk_s", "cores_bg_avg", "throttled_pct", "write_mb", "notifs", "cpu_s", "majflt", "state_lost"]


def per_app_rows(run: dict) -> list[dict]:
    """One row per app of a run: how often it was requested, how fast it came back from each tier,
    how much time it spent limited, what it still achieved while limited, and whether it lost state."""
    meta, steps = run["meta"], run["steps"]
    cfg = meta.get("config", {})
    cats, kinds = meta.get("categories") or {}, meta.get("kinds") or {}
    m_fg = (run.get("budget") or {}).get("m_fg_kb") or {}
    name = cfg.get("name") or run.get("path", "")
    out = []
    for app in meta.get("apps") or []:
        req = [s for s in steps if s["req"] == app]
        lat_bg = [s["launch"]["total_time_ms"] for s in req if s.get("was_throttled") and not s.get("was_shrunk")
                  and s["launch"].get("total_time_ms")]
        lat_sh = [s["launch"]["total_time_ms"] for s in req if s.get("was_shrunk") and s["launch"].get("total_time_ms")]
        ping_sh = [s["launch"]["ping_ms"] for s in req if s.get("was_shrunk") and s["launch"].get("ping_ms") is not None]
        t = {"fg": 0.0, "bg": 0.0, "shrunk": 0.0}
        cores, thr_num, thr_den = [], 0.0, 0.0
        mem_bg, mem_sh, pss_bg, pss_sh = [], [], [], []
        write_mb = notifs = cpu_s = majflt = 0.0
        lost = 0
        for s in steps:
            b = (s.get("bg") or {}).get(app)
            if not b:
                continue
            tier = b.get("tier", "fg") if s["req"] != app else "fg"
            t[tier] = t.get(tier, 0.0) + (b.get("wall_s") or 0)
            if tier != "fg":
                cores.append(b.get("cores") or 0)
                thr_num += b.get("throttled_ms", 0) / 1000
                thr_den += b.get("wall_s") or 0
                write_mb += b.get("write_mb", 0)
                notifs += b.get("notifications", 0)
                cpu_s += b.get("cg_cpu_ms", 0) / 1000
                majflt += b.get("majflt", 0)
                (mem_sh if tier == "shrunk" else mem_bg).append(b.get("mem_mb") or 0)
                if b.get("pss_mb") is not None:
                    (pss_sh if tier == "shrunk" else pss_bg).append(b["pss_mb"])
            if app in (s.get("state_lost") or {}):
                lost += 1
        out.append({
            "run": name, "app": app, "kind": kinds.get(app, "?"), "category": cats.get(app, "?"),
            "n_req": len(req), "n_cold": sum(1 for s in req if s["launch"].get("launch_state") == "COLD"),
            "lat_bg_p50_ms": pct(lat_bg, 0.5), "lat_shrunk_p50_ms": pct(lat_sh, 0.5),
            "ping_shrunk_p50_ms": pct(ping_sh, 0.5),
            "m_fg_mb": round(m_fg.get(app, 0) / 1024) if m_fg.get(app) else None,
            "mem_bg_avg_mb": round(sum(mem_bg) / len(mem_bg)) if mem_bg else None,
            "mem_shrunk_avg_mb": round(sum(mem_sh) / len(mem_sh)) if mem_sh else None,
            "pss_bg_avg_mb": round(sum(pss_bg) / len(pss_bg)) if pss_bg else None,
            "pss_shrunk_avg_mb": round(sum(pss_sh) / len(pss_sh)) if pss_sh else None,
            "t_fg_s": round(t["fg"]), "t_bg_s": round(t["bg"]), "t_shrunk_s": round(t["shrunk"]),
            "cores_bg_avg": round(sum(cores) / len(cores), 3) if cores else None,
            "throttled_pct": round(100 * thr_num / thr_den, 1) if thr_den else None,
            "write_mb": round(write_mb, 1), "notifs": int(notifs), "cpu_s": round(cpu_s, 1), "majflt": int(majflt),
            "state_lost": lost,
        })
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+")
    ap.add_argument("--csv")
    ap.add_argument("--agg", help="replicate-aggregated table (mean/sd per policy, scenario, eta) as CSV")
    ap.add_argument("--pareto", help="write Pareto points CSV")
    ap.add_argument("--plot", help="write Pareto PNG (needs matplotlib)")
    ap.add_argument("--x", choices=["saving", "eta"], default="saving")
    ap.add_argument("--all-cols", action="store_true")
    ap.add_argument("--per-app", metavar="CSV", nargs="?", const="-", help="per-app table (print, or write CSV)")
    ap.add_argument("--plots-dir", metavar="DIR", help="per-run timelines + latency CDFs as PNG (needs matplotlib)")
    ap.add_argument("--md", metavar="FILE", help="also write the tables as a Markdown report")
    args = ap.parse_args(argv)
    runs = [load(f) for f in args.files]
    rows = [summarize(r) for r in runs]
    rows.sort(key=lambda r: (str(r["policy"]), str(r.get("scenario")), r["eta"] or 0, r.get("seed") or 0))
    print_table(rows, None if args.all_cols else MAIN_COLS)
    seg = [x for r in runs for x in segment_rows(r)]
    if seg:
        print("\nper-scenario segments (day traces):")
        print_table(seg)
    per_app = [x for r in runs for x in per_app_rows(r)]
    if args.per_app is not None and per_app:
        if args.per_app == "-":
            print("\nper app:")
            print_table(per_app, PER_APP_COLS)
        else:
            with open(args.per_app, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=PER_APP_COLS)
                w.writeheader(); w.writerows(per_app)
            print(f"wrote {args.per_app}")
    if args.plots_dir:
        from .plots import plot_all
        made = plot_all(runs, args.plots_dir)
        print(f"wrote {len(made)} plot(s) to {args.plots_dir}")
    if args.md:
        from .report import write_markdown
        write_markdown(args.md, rows, aggregate(rows, EXTRA_AGG_COLS), per_app, seg, plots_dir=args.plots_dir)
        print(f"wrote {args.md}")
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
