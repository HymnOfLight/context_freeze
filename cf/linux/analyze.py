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
  quiet_swap_out_mb_per_min                 steady-state swap-out rate (system pswpout) while the background
                                            apps sit quiet (dwell samples after cfg quiet_after_s)
  quiet_bg_reclaim_mb_per_min               pages the kernel reclaimed from the background cgroups per minute
  quiet_zram_growth_mb_per_min              growth of the compressed zram store in that window (the RAM the
                                            squeezed pages still cost; zram_phys_avg_mb = its level)
  bg_squeeze_pct                            1 - PSS of the background apps at the end of the quiet window /
                                            their PSS when they were last in the foreground (per-process cfg)
  bg_pct_of_m_fg                            resident PSS of the background apps / their m_fg (warmup PSS+Swap,
                                            fixed across the cells) - the "x % of the active footprint" figure
  --per-proc                                per-process table: PSS in the foreground vs in the background

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
                  "psi_cpu_some_ms", "n_ping_timeout", "n_resident_avg", "work_checks", "work_fail",
                  "quiet_swap_out_mb_per_min", "quiet_bg_reclaim_mb_per_min", "quiet_bg_swap_growth_mb_per_min",
                  "quiet_zram_growth_mb_per_min", "bg_squeeze_pct", "bg_pct_of_m_fg", "bg_pss_quiet_avg_mb"]

MAIN_COLS = ["policy", "scenario", "eta", "seed", "cpu", "mem_frac", "T", "budget_mb", "bg_pss_avg_mb",
             "bg_pss_peak_mb", "swap_write_mb", "lat_p50_ms", "lat_p95_ms", "ping_p95_ms", "n_cold",
             "bg_cores_avg", "bg_throttled_pct", "download_mbps", "notif_per_h", "compute_cpu_s",
             "state_lost_events", "oom_kills", "work_checks", "work_fail", "budget_violations", "n_resident_avg",
             "mem_high_backoffs", "eta_min"]

# shown instead of MAIN_COLS when the run was recorded with cfg per_process (the squeeze experiment)
SQUEEZE_COLS = ["policy", "scenario", "eta", "seed", "cpu", "mem_frac", "T", "fg_pss_avg_mb", "bg_pss_quiet_avg_mb",
                "bg_squeeze_pct", "bg_pct_of_m_fg", "quiet_swap_out_mb_per_min", "quiet_bg_reclaim_mb_per_min",
                "quiet_bg_swap_growth_mb_per_min", "quiet_zram_growth_mb_per_min", "zram_phys_avg_mb", "quiet_s",
                "swap_write_mb", "lat_p50_ms", "lat_p95_ms",
                "ping_p95_ms", "n_cold", "bg_cores_avg", "bg_throttled_pct", "state_lost_events", "oom_kills",
                "work_checks", "work_fail", "mem_high_backoffs"]

SQUEEZE_APP_COLS = ["run", "app", "m_fg_mb", "bg_resid_mb", "bg_pct_of_m_fg", "bg_cpu_pct_core", "bg_throttled_pct",
                    "bg_reclaim_mb_per_min", "bg_swapin_mb_per_min", "n_req", "lat_p50_ms", "lat_p95_ms", "ping_p50_ms"]

PER_PROC_COLS = ["run", "app", "proc", "n_pids", "pss_fg_mb", "pss_bg_mb", "pss_bg_quiet_mb", "swap_bg_quiet_mb",
                 "squeeze_pct", "swap_growth_mb_per_min"]

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
        "work_checks": sum(1 for s in steps if s["launch"].get("work_state_ok") is not None),
        "work_fail": sum(1 for s in steps if s["launch"].get("work_state_ok") is False),
        "work_steps": sum(1 for s in steps if (s.get("work") or {}).get("status") == "ok"),
        "n_resident_avg": round(sum(len([a for a in s.get("resident", []) if a != s["req"]]) for s in steps)
                                / len(steps), 2) if steps else None,
        # thrash guard: how often a shrunk app's memory.high had to be raised (0 = the profile's limits held)
        "mem_high_backoffs": sum(len((s.get("actions") or {}).get("backoff") or {}) for s in steps),
        "mem_high_backoff_apps": ",".join(sorted({a for s in steps
                                                  for a in ((s.get("actions") or {}).get("backoff") or {})})),
    })
    row.update(quiet_metrics(run))
    # a budgeted cell whose budget is below the shrunk floor is identical to static (nothing can be resident)
    em = row["eta_min"]
    row["eta_feasible"] = None if em is None or row["policy"] in ("none", "static") else bool((row["eta"] or 0) >= em)
    return row


PER_APP_COLS = ["run", "app", "kind", "category", "n_req", "n_cold", "lat_bg_p50_ms", "lat_shrunk_p50_ms",
                "ping_shrunk_p50_ms", "m_fg_mb", "pss_bg_avg_mb", "pss_shrunk_avg_mb", "mem_bg_avg_mb",
                "mem_shrunk_avg_mb", "t_fg_s", "t_bg_s",
                "t_shrunk_s", "cores_bg_avg", "throttled_pct", "write_mb", "notifs", "cpu_s", "majflt", "state_lost",
                "work_checks", "work_fail"]


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
            "work_checks": sum(1 for s in req if s["launch"].get("work_state_ok") is not None),
            "work_fail": sum(1 for s in req if s["launch"].get("work_state_ok") is False),
        })
    return out


def _quiet_samples(step: dict, quiet_after_s: float) -> list[dict]:
    return [x for x in (step.get("timeline") or []) if (x.get("dt") or 0) >= quiet_after_s]


def quiet_metrics(run: dict) -> dict:
    """Steady-state numbers for the part of each dwell after cfg quiet_after_s, when the requested app
    has done its scripted work and the background apps just sit there: system swap-out rate, pages
    reclaimed from the background cgroups, growth of their swap, and how far their PSS went down
    compared with the last time each of them was in the foreground."""
    meta, steps = run["meta"], run["steps"]
    cfg = meta.get("config", {})
    quiet_after = float(cfg.get("quiet_after_s") or 30.0)
    never = set(cfg.get("never_throttle") or [])
    sw_pages = rc_pages = sw_growth = zr_growth = quiet_s = 0.0
    # foreground reference = PSS at the end of the app's own dwell; until it has been requested once,
    # the warmup sample (every app launched and left in front of the others) stands in
    fg_last_pss: dict[str, float] = {w["pkg"]: w["app"]["pss_kb"] for w in run.get("warmup") or []
                                     if (w.get("app") or {}).get("pss_kb")}
    squeeze, bg_quiet, fg_pss, of_mfg = [], [], [], []
    m_fg = (run.get("budget") or {}).get("m_fg_kb") or {}
    for s in steps:
        req = s["req"]
        qs = _quiet_samples(s, quiet_after)
        if len(qs) >= 2:
            a, b = qs[0], qs[-1]
            dt = (b["dt"] or 0) - (a["dt"] or 0)
            if dt > 0 and (a.get("system") or {}).get("pswpout") is not None and (b.get("system") or {}).get("pswpout") is not None:
                quiet_s += dt
                sw_pages += max(0, b["system"]["pswpout"] - a["system"]["pswpout"])
                za, zb = (a["system"].get("zram") or {}).get("mem_used_total"), (b["system"].get("zram") or {}).get("mem_used_total")
                if za is not None and zb is not None:
                    zr_growth += (zb - za) / 2 ** 20       # compressed RAM the swapped pages still occupy
                for app, bb in (b.get("apps") or {}).items():
                    aa = (a.get("apps") or {}).get(app) or {}
                    if app == req or app in never:
                        continue
                    if bb.get("cg_pgsteal") is not None and aa.get("cg_pgsteal") is not None:
                        rc_pages += max(0, bb["cg_pgsteal"] - aa["cg_pgsteal"])
                    if bb.get("cg_swap_mb") is not None and aa.get("cg_swap_mb") is not None:
                        sw_growth += bb["cg_swap_mb"] - aa["cg_swap_mb"]
        apps2 = (s.get("after_dwell") or {}).get("apps") or {}
        if req in apps2 and apps2[req].get("pss_kb") is not None:
            fg_pss.append(apps2[req]["pss_kb"] / 1024)
        bg_now = {app: d.get("pss_kb") or 0 for app, d in apps2.items() if app != req and app not in never}
        bg_quiet.append(sum(bg_now.values()) / 1024)
        den = sum(m_fg.get(app) or 0 for app in bg_now)
        if den > 0:
            of_mfg.append(sum(bg_now[app] for app in bg_now if m_fg.get(app)) / den)
        ref = {app: fg_last_pss[app] for app in bg_now if app in fg_last_pss}
        if ref and sum(ref.values()) > 0:
            squeeze.append(1 - sum(bg_now[app] for app in ref) / sum(ref.values()))
        if req in apps2 and apps2[req].get("pss_kb"):
            fg_last_pss[req] = apps2[req]["pss_kb"]
    mins = quiet_s / 60
    return {"quiet_s": round(quiet_s),
            "quiet_swap_out_mb_per_min": round(sw_pages * 4 / 1024 / mins, 2) if mins else None,
            "quiet_bg_reclaim_mb_per_min": round(rc_pages * 4 / 1024 / mins, 2) if mins else None,
            "quiet_bg_swap_growth_mb_per_min": round(sw_growth / mins, 2) if mins else None,
            "quiet_zram_growth_mb_per_min": round(zr_growth / mins, 2) if mins else None,
            "fg_pss_avg_mb": round(sum(fg_pss) / len(fg_pss)) if fg_pss else None,
            "bg_pss_quiet_avg_mb": round(sum(bg_quiet) / len(bg_quiet)) if bg_quiet else None,
            "bg_squeeze_pct": round(100 * sum(squeeze) / len(squeeze), 1) if squeeze else None,
            # resident PSS of the background apps / their m_fg (warmup PSS+Swap, the same denominator in
            # every cell of a run_squeeze.sh sweep) - the "10 % of the active footprint" number
            "bg_pct_of_m_fg": round(100 * sum(of_mfg) / len(of_mfg), 1) if of_mfg else None}


def per_process_rows(run: dict) -> list[dict]:
    """One row per (app, process name): PSS while the app was in the foreground (end of its own dwell,
    or the warmup sample when the trace never requested it),
    right after it was switched away (after_launch of the next step), at the end of a background dwell
    (quiet), swap at that point, the squeeze between the two, and how fast its swap grew during the
    quiet window.  Needs runs recorded with cfg per_process."""
    meta, steps = run["meta"], run["steps"]
    cfg = meta.get("config", {})
    quiet_after = float(cfg.get("quiet_after_s") or 30.0)
    name = cfg.get("name") or run.get("path", "")
    acc: dict[tuple[str, str], dict] = {}

    def slot(app, proc):
        return acc.setdefault((app, proc), {"pids": set(), "fg": [], "fg0": [], "bg0": [], "bgq": [], "swq": [],
                                             "sw_growth_mb": 0.0, "sw_s": 0.0})

    def by_name(procs):
        out: dict[str, list[float]] = {}
        for p in procs or []:
            out.setdefault(p["name"], [0.0, 0.0]); out[p["name"]][0] += p.get("pss_kb") or 0
            out[p["name"]][1] += p.get("swap_pss_kb") or 0
        return out

    for w in run.get("warmup") or []:
        for pn, (pss, _sw) in by_name((w.get("app") or {}).get("procs")).items():
            slot(w["pkg"], pn)["fg0"].append(pss / 1024)
    for s in steps:
        req = s["req"]
        for phase, key in (("after_launch", "bg0"), ("after_dwell", "bgq")):
            for app, d in ((s.get(phase) or {}).get("apps") or {}).items():
                procs = d.get("procs")
                if not procs:
                    continue
                for p in procs:
                    slot(app, p["name"])["pids"].add(p["pid"])
                for pn, (pss, sw) in by_name(procs).items():
                    sl = slot(app, pn)
                    if app == req:
                        if phase == "after_dwell":
                            sl["fg"].append(pss / 1024)
                    else:
                        sl[key].append(pss / 1024)
                        if phase == "after_dwell":
                            sl["swq"].append(sw / 1024)
        qs = _quiet_samples(s, quiet_after)
        if len(qs) >= 2:
            a, b = qs[0], qs[-1]
            dt = (b["dt"] or 0) - (a["dt"] or 0)
            for app, bb in (b.get("apps") or {}).items():
                if app == req or not bb.get("procs"):
                    continue
                aa = ((a.get("apps") or {}).get(app) or {}).get("procs") or {}
                growth: dict[str, float] = {}
                for pid, (pss, sw, pn) in bb["procs"].items():
                    if pid in aa:
                        growth[pn] = growth.get(pn, 0.0) + (sw - aa[pid][1]) / 1024
                for pn, g in growth.items():
                    sl = slot(app, pn); sl["sw_growth_mb"] += g; sl["sw_s"] += dt
    rows = []
    for (app, pn), sl in sorted(acc.items()):
        fgs = sl["fg"] or sl["fg0"]                      # own dwells, else the warmup sample
        fg = sum(fgs) / len(fgs) if fgs else None
        bgq = sum(sl["bgq"]) / len(sl["bgq"]) if sl["bgq"] else None
        rows.append({"run": name, "app": app, "proc": pn, "n_pids": len(sl["pids"]),
                     "pss_fg_mb": round(fg, 1) if fg is not None else None,
                     "pss_bg_mb": round(sum(sl["bg0"]) / len(sl["bg0"]), 1) if sl["bg0"] else None,
                     "pss_bg_quiet_mb": round(bgq, 1) if bgq is not None else None,
                     "swap_bg_quiet_mb": round(sum(sl["swq"]) / len(sl["swq"]), 1) if sl["swq"] else None,
                     "squeeze_pct": round(100 * (1 - bgq / fg), 1) if fg and bgq is not None else None,
                     "swap_growth_mb_per_min": round(sl["sw_growth_mb"] / (sl["sw_s"] / 60), 2) if sl["sw_s"] else None})
    return rows


def squeeze_app_rows(run: dict) -> list[dict]:
    """One row per app of a run with the five squeeze figures: active footprint (m_fg = warmup
    PSS+Swap), resident PSS while in the background (end of the dwells), CPU it got there as % of one
    core, memory the kernel took from it per minute (pgsteal) and paged back (major faults), and how
    fast it came back to the foreground."""
    meta, steps = run["meta"], run["steps"]
    cfg = meta.get("config", {})
    name = cfg.get("name") or run.get("path", "")
    m_fg = (run.get("budget") or {}).get("m_fg_kb") or {}
    out = []
    for app in meta.get("apps") or []:
        resid, cores, thr_num, thr_den, rc, swin, wall = [], [], 0.0, 0.0, 0.0, 0.0, 0.0
        has_rc = False
        for s in steps:
            if s["req"] == app:
                continue
            d = (s.get("after_dwell") or {}).get("apps", {}).get(app)
            if d and d.get("pss_kb") is not None:
                resid.append(d["pss_kb"] / 1024)
            b = (s.get("bg") or {}).get(app)
            if not b:
                continue
            w = b.get("wall_s") or 0
            wall += w
            if b.get("cores") is not None:
                cores.append(b["cores"])
            thr_num += b.get("throttled_ms", 0) / 1000; thr_den += w
            if b.get("reclaim_mb") is not None:
                rc += b["reclaim_mb"]; has_rc = True
            swin += b.get("majflt", 0) * 4 / 1024
        req = [s for s in steps if s["req"] == app and s["launch"].get("total_time_ms")]
        lat = [s["launch"]["total_time_ms"] for s in req]
        ping = [s["launch"]["ping_ms"] for s in req if s["launch"].get("ping_ms") is not None]
        mfg = (m_fg.get(app) or 0) / 1024
        br = sum(resid) / len(resid) if resid else None
        out.append({"run": name, "app": app, "m_fg_mb": round(mfg) if mfg else None,
                    "bg_resid_mb": round(br) if br is not None else None,
                    "bg_pct_of_m_fg": round(100 * br / mfg, 1) if br is not None and mfg else None,
                    "bg_cpu_pct_core": round(100 * sum(cores) / len(cores), 1) if cores else None,
                    "bg_throttled_pct": round(100 * thr_num / thr_den, 1) if thr_den else None,
                    "bg_reclaim_mb_per_min": round(rc / (wall / 60), 1) if wall and has_rc else None,
                    "bg_swapin_mb_per_min": round(swin / (wall / 60), 1) if wall else None,
                    "n_req": len(req), "lat_p50_ms": pct(lat, 0.5), "lat_p95_ms": pct(lat, 0.95),
                    "ping_p50_ms": pct(ping, 0.5)})
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
    ap.add_argument("--per-proc", metavar="CSV", nargs="?", const="-",
                    help="per-process fg/bg PSS table (runs recorded with per_process; print, or write CSV)")
    ap.add_argument("--squeeze-apps", metavar="CSV", nargs="?", const="-",
                    help="per-app squeeze table: m_fg, background resident, CPU %% of a core, reclaim / swap-in per minute, "
                         "switch-back latency (print, or write CSV)")
    ap.add_argument("--plots-dir", metavar="DIR", help="per-run timelines + latency CDFs as PNG (needs matplotlib)")
    ap.add_argument("--md", metavar="FILE", help="also write the tables as a Markdown report")
    ap.add_argument("--xlsx", metavar="FILE", help="Excel workbook of the squeeze tables (per_process runs; needs openpyxl)")
    args = ap.parse_args(argv)
    runs = [load(f) for f in args.files]
    rows = [summarize(r) for r in runs]
    rows.sort(key=lambda r: (str(r["policy"]), str(r.get("scenario")), r["eta"] or 0, r.get("seed") or 0))
    squeeze_mode = all(r["meta"].get("config", {}).get("per_process") for r in runs)
    print_table(rows, None if args.all_cols else (SQUEEZE_COLS if squeeze_mode else MAIN_COLS))
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
    per_proc = [x for r in runs for x in per_process_rows(r)]
    if args.per_proc is not None and per_proc:
        if args.per_proc == "-":
            print("\nper process:")
            print_table(per_proc, PER_PROC_COLS)
        else:
            with open(args.per_proc, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=PER_PROC_COLS)
                w.writeheader(); w.writerows(per_proc)
            print(f"wrote {args.per_proc}")
    sq_apps = [x for r in runs for x in squeeze_app_rows(r)] if squeeze_mode else []
    if args.squeeze_apps is not None and sq_apps:
        if args.squeeze_apps == "-":
            print("\nper app (squeeze figures):")
            print_table(sq_apps, SQUEEZE_APP_COLS)
        else:
            with open(args.squeeze_apps, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=SQUEEZE_APP_COLS)
                w.writeheader(); w.writerows(sq_apps)
            print(f"wrote {args.squeeze_apps}")
    if args.plots_dir:
        from .plots import plot_all
        made = plot_all(runs, args.plots_dir)
        print(f"wrote {len(made)} plot(s) to {args.plots_dir}")
    if args.md:
        from .report import write_markdown
        write_markdown(args.md, rows, aggregate(rows, EXTRA_AGG_COLS), per_app, seg, plots_dir=args.plots_dir,
                       per_proc=per_proc if squeeze_mode else None,
                       main_cols=SQUEEZE_COLS if squeeze_mode else MAIN_COLS, squeeze_apps=sq_apps or None)
        print(f"wrote {args.md}")
    if args.xlsx:
        if not squeeze_mode:
            print("--xlsx: only for runs recorded with per_process (run_squeeze.sh)")
        else:
            try:
                from .xlsx import write_xlsx
                write_xlsx(runs, args.xlsx)
                print(f"wrote {args.xlsx}")
            except ImportError:
                print("--xlsx needs openpyxl (pip install openpyxl)")
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
