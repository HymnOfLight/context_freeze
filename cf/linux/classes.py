"""Three-class experiment (manual §6.9): the same question for three kinds of applications -
what does limiting the background cost?

  interactive  how long a call / switch takes to answer and how smooth the app is right afterwards:
               response time (window activation + first event-loop answer), the _NET_WM_PING series
               of the responsiveness probe (stalls > 100 ms), the input read-back time of the
               scripted-work oracle (select all + copy until the text is on the clipboard)
  compute      a fixed job repeated for ever (cf.linux.bench_app numeric / compress): time per job and
               jobs per minute while the app is in the foreground vs in the background
  download     the same for a download: MB/s and the TCP connect round trip, foreground vs background

One row per (run, app); runs with policy `none` are the baseline the `x` / `%` columns refer to.

    python3 -m cf.linux.classes results/arm64_classes/*.jsonl --out results/arm64_classes

writes classes_interactive.csv, classes_compute.csv, classes_download.csv, classes_report.md,
classes_tables.xlsx (needs openpyxl) and plots/classes.png + plots/<run>_bench.png (needs matplotlib).
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import Counter
from typing import Optional

from ..analyze import load, pct

STALL_MS = 100.0          # a ping answered later than this is a stall the user notices
MERGE_GAP_S = 60.0        # background dwells further apart than this are separate intervals (resume)
INSIDE_FRAC = 0.9         # a job / download second counts for a stretch when this much of it lies inside

INTERACTIVE_COLS = ["run", "policy", "app", "n_calls", "n_limited", "n_cold", "resp_p50_ms", "resp_p95_ms", "resp_max_ms",
                    "resp_p50_x", "activate_p50_ms", "ping_p50_ms", "probe_n", "probe_p50_ms", "probe_p95_ms",
                    "probe_max_ms", "stall_pct", "echo_n", "echo_p50_ms", "echo_max_ms", "work_checks", "work_fail"]
COMPUTE_COLS = ["run", "policy", "app", "fg_s", "fg_jobs", "fg_job_p50_ms", "fg_job_p95_ms", "fg_jobs_per_min", "fg_cpu_pct",
                "bg_s", "bg_jobs", "bg_job_p50_ms", "bg_job_p95_ms", "bg_jobs_per_min", "bg_cpu_pct", "slowdown_x",
                "bg_throughput_pct", "bg_s_per_100_jobs", "bg_vs_none_x", "bg_tiers", "bad_jobs"]
DOWNLOAD_COLS = ["run", "policy", "app", "fg_s", "fg_mbps", "fg_mbps_p10", "fg_rtt_p50_ms", "fg_rtt_p95_ms", "fg_ext_rtt_p50_ms",
                 "fg_ext_rtt_p95_ms", "fg_wake_p50_ms", "fg_wake_p95_ms", "bg_s", "bg_mbps", "bg_mbps_p10", "bg_rtt_p50_ms",
                 "bg_rtt_p95_ms", "bg_ext_rtt_p50_ms", "bg_ext_rtt_p95_ms", "bg_wake_p50_ms", "bg_wake_p95_ms", "bg_speed_pct",
                 "bg_vs_none_pct", "rtt_lost_pct", "errors", "bg_tiers"]

INTERACTIVE_CN = [("run", "格"), ("app", "应用"), ("n_calls", "呼叫次数"), ("n_limited", "其中从受限后台切回"),
                  ("resp_p50_ms", "响应 P50 (ms)"), ("resp_p95_ms", "响应 P95 (ms)"), ("resp_max_ms", "响应最大 (ms)"),
                  ("resp_p50_x", "响应 P50 相对不限制 (倍)"), ("activate_p50_ms", "窗口激活 P50 (ms)"),
                  ("ping_p50_ms", "首次应答 P50 (ms)"), ("probe_n", "顺滑探测次数"), ("probe_p50_ms", "探测应答 P50 (ms)"),
                  ("probe_p95_ms", "探测应答 P95 (ms)"), ("probe_max_ms", "探测应答最大 (ms)"),
                  ("stall_pct", f"卡顿占比 (>{STALL_MS:g} ms, %)"), ("echo_n", "输入回读次数"), ("echo_p50_ms", "输入回读 P50 (ms)"),
                  ("echo_max_ms", "输入回读最大 (ms)"), ("work_checks", "内容核对次数"), ("work_fail", "内容丢失"),
                  ("n_cold", "冷启动 (不计入)")]
COMPUTE_CN = [("run", "格"), ("app", "应用"), ("fg_job_p50_ms", "前台 单任务 P50 (ms)"), ("fg_job_p95_ms", "前台 单任务 P95 (ms)"),
              ("fg_jobs_per_min", "前台 每分钟任务数"), ("bg_job_p50_ms", "后台 单任务 P50 (ms)"),
              ("bg_job_p95_ms", "后台 单任务 P95 (ms)"), ("bg_jobs_per_min", "后台 每分钟任务数"),
              ("slowdown_x", "后台/前台 单任务时延 (倍)"), ("bg_throughput_pct", "后台吞吐占前台 (%)"),
              ("bg_s_per_100_jobs", "后台做完 100 个任务 (s)"), ("bg_vs_none_x", "后台时延相对不限制 (倍)"),
              ("fg_cpu_pct", "前台 任务内 CPU 占比 (%)"), ("bg_cpu_pct", "后台 任务内 CPU 占比 (%)"),
              ("fg_jobs", "前台任务数"), ("bg_jobs", "后台任务数"), ("fg_s", "前台观测 (s)"), ("bg_s", "后台观测 (s)"),
              ("bg_tiers", "后台档位"), ("bad_jobs", "结果校验失败")]
DOWNLOAD_CN = [("run", "格"), ("app", "应用"), ("fg_mbps", "前台 下载速度 (MB/s)"), ("fg_mbps_p10", "前台 速度 P10 (MB/s)"),
               ("bg_mbps", "后台 下载速度 (MB/s)"), ("bg_mbps_p10", "后台 速度 P10 (MB/s)"), ("bg_speed_pct", "后台速度占前台 (%)"),
               ("bg_vs_none_pct", "后台速度相对不限制 (%)"), ("fg_rtt_p50_ms", "前台 建连时延 P50 (ms)"),
               ("fg_rtt_p95_ms", "前台 建连时延 P95 (ms)"), ("bg_rtt_p50_ms", "后台 建连时延 P50 (ms)"),
               ("bg_rtt_p95_ms", "后台 建连时延 P95 (ms)"), ("fg_ext_rtt_p50_ms", "前台 外网时延 P50 (ms)"),
               ("fg_ext_rtt_p95_ms", "前台 外网时延 P95 (ms)"), ("bg_ext_rtt_p50_ms", "后台 外网时延 P50 (ms)"),
               ("bg_ext_rtt_p95_ms", "后台 外网时延 P95 (ms)"), ("fg_wake_p50_ms", "前台 处理延迟 P50 (ms)"),
               ("fg_wake_p95_ms", "前台 处理延迟 P95 (ms)"), ("bg_wake_p50_ms", "后台 处理延迟 P50 (ms)"),
               ("bg_wake_p95_ms", "后台 处理延迟 P95 (ms)"), ("rtt_lost_pct", "探测超时 (%)"), ("errors", "下载出错次数"),
               ("fg_s", "前台观测 (s)"), ("bg_s", "后台观测 (s)"), ("bg_tiers", "后台档位")]

NOTES = [
    "呼叫与切换: 响应 = 把应用切到前台的总耗时 (窗口激活 + 事件循环第一次应答; 不应答 _NET_WM_PING 的应用只有窗口激活). "
    "切回之前限制已解除, 所以它量的是 '被压过的应用醒来要多久' (缺页换回 + 排队), 不是限速本身. 第 0 步 (预热后的第一次切换, "
    "此时还没有任何应用被限制) 和冷启动不计入.",
    f"顺滑: 切到前台后在 settle 时间内连续 ping 事件循环 (探测应答), 超过 {STALL_MS:g} ms 或超时记一次卡顿; 输入回读 = 发出 "
    "全选+复制 到剪贴板拿到文本的时间 (两次按键延时 + 轮询约 0.2 s 是固定底噪, 看格与格的差). 不应答 ping 的应用 (微信等) 只有输入回读.",
    "计算型: 同一个固定任务循环做 (科学计算 = LU 分解求解 + FFT + n 体, 一个工作进程; 压缩解压 = zlib -6 压 8 MB 再解压校验, 两个工作进程). "
    "前台 = 该应用是当前请求的应用的停留期; 后台 = 其它应用在前台的停留期 (只统计至少 90 % 的时间落在同一档位里的任务). "
    "任务内 CPU 占比 = 工作进程的 CPU 时间 / 任务墙钟时间, 低说明任务大部分时间在等 CPU 配额或等缺页.",
    "下载型: HTTP Range 分片下载, 每片做 sha256 并写盘; 速度 = 字节数 / 用时, P10 = 最慢的 10 % 的秒. 建连时延 = 下载进程自己发起 "
    "TCP connect 到下载源的耗时, 每 0.5 s 一次 (网络本身的往返时延, 限制后台基本不改变它); 外网时延 = 同样的探测打到 CF_BENCH_RTT_HOST. "
    "处理延迟 = 探测线程比预定时刻晚了多久才被调度到 —— 一个发给后台应用的包到了以后, 应用要等这么久才轮到 CPU 去处理它 "
    "(cpu.max 10 ms / 100 ms 时最多约 90 ms, 被前台抢占时更久); 应用实际感受到的网络时延 = 建连时延 + 处理延迟.",
    "'相对不限制' 两列以 policy=none 的那一格的同一应用为基准.",
]


def _name(run: dict) -> str:
    cfg = run["meta"].get("config", {})
    return cfg.get("name") or os.path.basename(str(run.get("path", "run"))).replace(".jsonl", "")


def _policy(run: dict) -> str:
    return str(run["meta"].get("config", {}).get("policy") or "")


def _r(v: Optional[float], nd: int = 1) -> Optional[float]:
    return None if v is None else round(v, nd)


def classes_of(run: dict) -> dict[str, list[str]]:
    """{"interactive": [...], "compute": [...], "download": [...]} - the config's `classes`, restricted
    to the apps of the run; without it: bench apps by category, everything else interactive."""
    meta = run["meta"]
    apps = list(meta.get("apps") or [])
    kinds, cats = meta.get("kinds") or {}, meta.get("categories") or {}
    cfg = (meta.get("config") or {}).get("classes") or {}
    out = {k: [a for a in cfg.get(k) or [] if a in apps] for k in ("interactive", "compute", "download")}
    if not any(out.values()):
        for a in apps:
            cat = cats.get(a)
            cat = cat[0] if isinstance(cat, (list, tuple)) else cat
            if kinds.get(a) == "bench":
                out["download" if cat == "download" else "compute"].append(a)
            else:
                out["interactive"].append(a)
    return out


# ---------------------------------------------------------------------------- interactive
def _calls(run: dict, apps: list[str]) -> list[dict]:
    """Steps that requested one of `apps`, without step 0: the first switch after the warmup finds
    every app still unlimited in every cell, so it says nothing about the limits."""
    return [s for s in run["steps"] if s["req"] in apps and s["t"] > 0]


def _interactive_row(run: dict, label: str, apps: list[str]) -> dict:
    steps = _calls(run, apps)
    calls = [s for s in steps if s["launch"].get("total_time_ms") is not None and s["launch"].get("launch_state") != "COLD"]
    resp = [float(s["launch"]["total_time_ms"]) for s in calls]
    act = [float(s["launch"]["activate_ms"]) for s in calls if s["launch"].get("activate_ms") is not None]
    ping = [float(s["launch"]["ping_ms"]) for s in calls if s["launch"].get("ping_ms") is not None]
    probe, stalls = [], 0
    for s in steps:
        sm = s["launch"].get("smooth") or {}
        if not sm.get("supported"):
            continue
        timeout_ms = 1000.0 * float(((run["meta"].get("config") or {}).get("smooth_probe") or {}).get("timeout_s", 1.0))
        for ms in sm.get("pings_ms") or []:
            v = timeout_ms if ms is None else float(ms)
            probe.append(v)
            stalls += v > STALL_MS
    echo = [float(s["launch"]["work_check"]["after"]["read_ms"]) for s in steps
            if ((s["launch"].get("work_check") or {}).get("after") or {}).get("read_ms") is not None]
    checks = [s["launch"].get("work_state_ok") for s in steps if s["launch"].get("work_state_ok") is not None]
    return {"run": _name(run), "policy": _policy(run), "app": label, "n_calls": len(calls),
            "n_limited": sum(1 for s in calls if s.get("was_shrunk") or s.get("was_throttled")),
            "n_cold": sum(1 for s in steps if s["launch"].get("launch_state") == "COLD"),
            "resp_p50_ms": _r(pct(resp, 0.5)), "resp_p95_ms": _r(pct(resp, 0.95)), "resp_max_ms": _r(max(resp) if resp else None),
            "resp_p50_x": None, "activate_p50_ms": _r(pct(act, 0.5)), "ping_p50_ms": _r(pct(ping, 0.5), 2),
            "probe_n": len(probe), "probe_p50_ms": _r(pct(probe, 0.5), 2), "probe_p95_ms": _r(pct(probe, 0.95), 2),
            "probe_max_ms": _r(max(probe) if probe else None, 2),
            "stall_pct": _r(100.0 * stalls / len(probe)) if probe else None,
            "echo_n": len(echo), "echo_p50_ms": _r(pct(echo, 0.5)), "echo_max_ms": _r(max(echo) if echo else None),
            "work_checks": len(checks), "work_fail": sum(1 for c in checks if c is False)}


def interactive_rows(run: dict) -> list[dict]:
    apps = classes_of(run)["interactive"]
    rows = [_interactive_row(run, a, [a]) for a in apps]
    if len(apps) > 1:
        rows.append(_interactive_row(run, "ALL", apps))
    return rows


# ---------------------------------------------------------------------------- bench apps
def bench_events(run: dict, app: str) -> list[dict]:
    """The app's bench records with absolute times: {"T": end (epoch s), "k": ..., ...}."""
    out = []
    for s in run["steps"]:
        t0 = s.get("t_dwell")
        if t0 is None:
            continue
        for r in (s.get("bench") or {}).get(app) or []:
            out.append({**r, "T": t0 + r["dt"]})
    out.sort(key=lambda r: r["T"])
    return out


def intervals(run: dict, app: str) -> list[dict]:
    """Stretches of time in which `app` kept one position: "fg" = the dwell of a step that requested
    it, "bg" = consecutive dwells of steps that requested something else (the limits of a background
    app are only touched when it is requested, so the switches in between belong to the stretch).
    [{"pos", "t0", "t1", "tiers": [tier per step]}]"""
    out: list[dict] = []
    for s in run["steps"]:
        t0 = s.get("t_dwell")
        b = (s.get("bg") or {}).get(app)
        if t0 is None or not b:
            continue
        t1 = t0 + float(b.get("wall_s") or s.get("dwell_s") or 0)
        pos = "fg" if s["req"] == app else "bg"
        tier = b.get("tier") or (s.get("tiers") or {}).get(app) or "fg"
        last = out[-1] if out else None
        if last and pos == "bg" and last["pos"] == "bg" and t0 - last["t1"] < MERGE_GAP_S and last["step"] == s["t"] - 1:
            last["t1"], last["step"] = t1, s["t"]
            last["tiers"].append(tier)
        else:
            out.append({"pos": pos, "t0": t0, "t1": t1, "tiers": [tier], "step": s["t"]})
    return out


def _inside(ivs: list[dict], pos: str, start: float, end: float) -> bool:
    """Does [start, end] belong to a stretch of `pos`?  Short records must lie inside; a job that took
    most of a background stretch (a starved app) still counts when 90 % of it does - otherwise
    exactly the slowest jobs, which end just after the app was called back, would be dropped."""
    if end <= start:
        return any(iv["pos"] == pos and iv["t0"] <= end <= iv["t1"] for iv in ivs)
    dur = end - start
    return any(iv["pos"] == pos and min(end, iv["t1"]) - max(start, iv["t0"]) >= INSIDE_FRAC * dur for iv in ivs)


def _seconds(ivs: list[dict], pos: str) -> float:
    return sum(iv["t1"] - iv["t0"] for iv in ivs if iv["pos"] == pos)


def _tiers(ivs: list[dict]) -> str:
    c = Counter(t for iv in ivs if iv["pos"] == "bg" for t in iv["tiers"])
    return ", ".join(f"{k}×{n}" for k, n in sorted(c.items(), key=lambda kv: -kv[1]))


def compute_rows(run: dict) -> list[dict]:
    rows = []
    for app in classes_of(run)["compute"]:
        ev = [e for e in bench_events(run, app) if e.get("k") == "job"]
        ivs = intervals(run, app)
        row = {"run": _name(run), "policy": _policy(run), "app": app, "bg_tiers": _tiers(ivs),
               "bad_jobs": sum(1 for e in ev if e.get("ok") is False), "bg_vs_none_x": None}
        for pos in ("fg", "bg"):
            jobs = [e for e in ev if _inside(ivs, pos, e["T"] - e["ms"] / 1000.0, e["T"])]
            ms = [float(e["ms"]) for e in jobs]
            sec = _seconds(ivs, pos)
            row.update({f"{pos}_s": _r(sec), f"{pos}_jobs": len(jobs), f"{pos}_job_p50_ms": _r(pct(ms, 0.5)),
                        f"{pos}_job_p95_ms": _r(pct(ms, 0.95)),
                        f"{pos}_jobs_per_min": _r(60.0 * len(jobs) / sec) if sec > 0 else None,
                        f"{pos}_cpu_pct": _r(100.0 * sum(float(e.get("cpu") or 0) for e in jobs) / sum(ms)) if sum(ms) > 0 else None})
        f50, b50 = row["fg_job_p50_ms"], row["bg_job_p50_ms"]
        row["slowdown_x"] = _r(b50 / f50, 2) if f50 and b50 else None
        ft, bt = row["fg_jobs_per_min"], row["bg_jobs_per_min"]
        row["bg_throughput_pct"] = _r(100.0 * bt / ft) if ft and bt is not None else None
        row["bg_s_per_100_jobs"] = _r(6000.0 / bt) if bt else None
        rows.append(row)
    return rows


def download_rows(run: dict) -> list[dict]:
    rows = []
    for app in classes_of(run)["download"]:
        ev = bench_events(run, app)
        ivs = intervals(run, app)
        rtt_all = [e for e in ev if e.get("k") == "rtt"]
        row = {"run": _name(run), "policy": _policy(run), "app": app, "bg_tiers": _tiers(ivs),
               "errors": sum(1 for e in ev if e.get("k") == "err"), "bg_vs_none_pct": None,
               "rtt_lost_pct": _r(100.0 * sum(1 for e in rtt_all if e.get("ms") is None) / len(rtt_all)) if rtt_all else None}
        for pos in ("fg", "bg"):
            dl = [e for e in ev if e.get("k") == "dl" and e.get("s") and _inside(ivs, pos, e["T"] - e["s"], e["T"])]
            sec = sum(e["s"] for e in dl)
            per = [e["b"] / 2 ** 20 / e["s"] for e in dl]
            row.update({f"{pos}_s": _r(_seconds(ivs, pos)),
                        f"{pos}_mbps": _r(sum(e["b"] for e in dl) / 2 ** 20 / sec, 2) if sec > 0 else None,
                        f"{pos}_mbps_p10": _r(pct(per, 0.1), 2)})
            for label, key in (("dl", "rtt"), ("ext", "ext_rtt")):
                ms = [float(e["ms"]) for e in rtt_all if e.get("to") == label and e.get("ms") is not None
                      and _inside(ivs, pos, e["T"] - e["ms"] / 1000.0, e["T"])]
                row[f"{pos}_{key}_p50_ms"], row[f"{pos}_{key}_p95_ms"] = _r(pct(ms, 0.5), 2), _r(pct(ms, 0.95), 2)
            late = [float(e["late"]) for e in rtt_all if e.get("to") == "dl" and e.get("late") is not None
                    and _inside(ivs, pos, e["T"], e["T"])]
            row[f"{pos}_wake_p50_ms"], row[f"{pos}_wake_p95_ms"] = _r(pct(late, 0.5), 2), _r(pct(late, 0.95), 2)
        f, b = row["fg_mbps"], row["bg_mbps"]
        row["bg_speed_pct"] = _r(100.0 * b / f) if f and b is not None else None
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------- all runs together
def _baseline(runs: list[dict], run: dict) -> Optional[dict]:
    """The policy-none run the `x` / `%` columns compare to: same seed when there is one."""
    seed = (run["meta"].get("config") or {}).get("trace", {}).get("seed")
    none = [r for r in runs if _policy(r) == "none"]
    same = [r for r in none if (r["meta"].get("config") or {}).get("trace", {}).get("seed") == seed]
    return (same or none or [None])[0]


def tables(runs: list[dict]) -> dict[str, list[dict]]:
    """{"interactive": rows, "compute": rows, "download": rows} over all runs, baseline columns filled."""
    per = {id(r): {"interactive": interactive_rows(r), "compute": compute_rows(r), "download": download_rows(r)} for r in runs}
    for r in runs:
        base = _baseline(runs, r)
        if base is None:
            continue
        ref = {k: {x["app"]: x for x in per[id(base)][k]} for k in ("interactive", "compute", "download")}
        for row in per[id(r)]["interactive"]:
            b = ref["interactive"].get(row["app"]) or {}
            if row["resp_p50_ms"] and b.get("resp_p50_ms"):
                row["resp_p50_x"] = _r(row["resp_p50_ms"] / b["resp_p50_ms"], 2)
        for row in per[id(r)]["compute"]:
            b = ref["compute"].get(row["app"]) or {}
            if row["bg_job_p50_ms"] and b.get("bg_job_p50_ms"):
                row["bg_vs_none_x"] = _r(row["bg_job_p50_ms"] / b["bg_job_p50_ms"], 2)
        for row in per[id(r)]["download"]:
            b = ref["download"].get(row["app"]) or {}
            if row["bg_mbps"] is not None and b.get("bg_mbps"):
                row["bg_vs_none_pct"] = _r(100.0 * row["bg_mbps"] / b["bg_mbps"])
    return {k: [x for r in runs for x in per[id(r)][k]] for k in ("interactive", "compute", "download")}


def _cn(app: str) -> str:
    from .xlsx import APP_CN
    return "全部交互应用" if app == "ALL" else APP_CN.get(app, app)


def _cell(v) -> str:
    return "–" if v is None or v == "" else (f"{v:g}" if isinstance(v, float) else str(v))


def _md_table(rows: list[dict], cols: list[tuple[str, str]]) -> list[str]:
    out = ["| " + " | ".join(cn for _, cn in cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        out.append("| " + " | ".join(_cn(r[k]) if k == "app" else _cell(r.get(k)) for k, _ in cols) + " |")
    return out


def _limits(run: dict) -> str:
    cfg = run["meta"].get("config") or {}
    if cfg.get("policy") == "none":
        return "不限制 (基准)"
    t = cfg.get("throttle") or {}
    return (f"cpu_mode {t.get('cpu_mode')}, cpu_cores {t.get('cpu_cores')}, cpu_weight {t.get('cpu_weight')}, "
            f"memory.high = {t.get('mem_frac')} × m_fg, squeeze_rounds {t.get('squeeze_rounds')}")


def report_md(runs: list[dict], tb: Optional[dict] = None) -> str:
    tb = tb or tables(runs)
    lines = ["# 三类应用实验: 呼叫与切换 / 计算型 / 下载型", ""]
    for r in runs:
        cfg = r["meta"].get("config") or {}
        cl = classes_of(r)
        lines.append(f"- **{_name(r)}**: {_limits(r)}; T={len(r['steps'])} 步, 停留 {cfg.get('dwell_s')} s"
                     + ("" if r.get("finished") else " (未跑完)")
                     + f"; 交互 {len(cl['interactive'])} 个, 计算 {len(cl['compute'])} 个, 下载 {len(cl['download'])} 个")
    short = {"interactive": ["run", "app", "n_calls", "resp_p50_ms", "resp_p95_ms", "resp_max_ms", "resp_p50_x", "probe_p95_ms",
                             "stall_pct", "echo_p50_ms", "echo_max_ms", "work_fail"],
             "compute": ["run", "app", "fg_job_p50_ms", "bg_job_p50_ms", "bg_job_p95_ms", "slowdown_x", "fg_jobs_per_min",
                         "bg_jobs_per_min", "bg_throughput_pct", "bg_s_per_100_jobs", "bg_vs_none_x", "bg_cpu_pct"],
             "download": ["run", "app", "fg_mbps", "bg_mbps", "bg_mbps_p10", "bg_speed_pct", "bg_vs_none_pct", "fg_rtt_p50_ms",
                          "bg_rtt_p50_ms", "bg_rtt_p95_ms", "bg_ext_rtt_p50_ms", "bg_ext_rtt_p95_ms", "fg_wake_p95_ms",
                          "bg_wake_p50_ms", "bg_wake_p95_ms", "rtt_lost_pct"]}
    for key, title, cn in (("interactive", "一、呼叫与切换的响应", INTERACTIVE_CN), ("compute", "二、计算型应用: 前台 vs 后台", COMPUTE_CN),
                           ("download", "三、下载型应用: 前台 vs 后台", DOWNLOAD_CN)):
        lines += ["", f"## {title}", ""]
        if not tb[key]:
            lines.append("(这次运行没有这一类应用)")
            continue
        names = dict(cn)
        lines += _md_table(tb[key], [(k, names[k]) for k in short[key]])
    lines += ["", "## 说明", ""] + [f"- {n}" for n in NOTES]
    lines += ["", "完整的列见 classes_interactive.csv / classes_compute.csv / classes_download.csv 和 classes_tables.xlsx."]
    return "\n".join(lines) + "\n"


def write_csv(rows: list[dict], cols: list[str], path: str) -> str:
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return path


def write_xlsx(runs: list[dict], path: str, tb: Optional[dict] = None) -> str:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    tb = tb or tables(runs)
    thin = Side(style="thin", color="BBBBBB")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    wb = Workbook()
    first = True
    for key, title, cn in (("interactive", "呼叫与切换", INTERACTIVE_CN), ("compute", "计算型", COMPUTE_CN),
                           ("download", "下载型", DOWNLOAD_CN)):
        ws = wb.active if first else wb.create_sheet()
        first = False
        ws.title = title
        ws.append([c for _, c in cn])
        for r in tb[key]:
            ws.append([_cn(r[k]) if k == "app" else r.get(k) for k, _ in cn])
        for row in ws.iter_rows():
            for c in row:
                c.border = border
                c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        for c in ws[1]:
            c.font = Font(bold=True)
            c.fill = PatternFill("solid", fgColor="DDEBF7")
        for col in ws.columns:
            w = max(len(str(c.value)) if c.value is not None else 0 for c in col)
            ws.column_dimensions[get_column_letter(col[0].column)].width = min(max(10, w * 1.1 + 2), 30)
        ws.row_dimensions[1].height = 48
        ws.freeze_panes = "C2"
    ws = wb.create_sheet("说明")
    for r in runs:
        ws.append([f"{_name(r)}: {_limits(r)}"])
    for n in NOTES:
        ws.append([n])
    ws.column_dimensions["A"].width = 160
    for row in ws.iter_rows():
        row[0].alignment = Alignment(wrap_text=True, vertical="top")
    wb.save(path)
    return path


# ---------------------------------------------------------------------------- figures
def _label(run_name: str) -> str:
    return run_name.split("_classes")[0] if "_classes" in run_name else run_name


def plot_classes(runs: list[dict], path: str, tb: Optional[dict] = None) -> bool:
    """One figure, three rows (interactive / compute / download), two panels each."""
    from .plots import _plt, _use_cjk_font
    tb = tb or tables(runs)
    if not any(tb.values()):
        return False
    plt = _plt()
    zh = _use_cjk_font()
    T = (lambda z, e: z) if zh else (lambda z, e: e)
    names = [_name(r) for r in runs]
    colors = ["#9e9e9e", "#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e"]
    col = {n: colors[i % len(colors)] for i, n in enumerate(names)}
    app_l = (lambda a: _cn(a)) if zh else (lambda a: "all" if a == "ALL" else a.replace("bench_", ""))
    fig, axes = plt.subplots(3, 2, figsize=(13, 11.5))

    def grouped(ax, rows, key, err_key=None, title="", ylabel="", log=False):
        apps = list(dict.fromkeys(r["app"] for r in rows))
        w = 0.8 / max(1, len(names))
        any_bar = False
        for i, n in enumerate(names):
            by = {r["app"]: r for r in rows if r["run"] == n}
            xs, ys, es = [], [], []
            for j, a in enumerate(apps):
                v = (by.get(a) or {}).get(key)
                if v is None:
                    continue
                xs.append(j + (i - (len(names) - 1) / 2) * w)
                ys.append(v)
                e = (by.get(a) or {}).get(err_key) if err_key else None
                es.append(max(0.0, e - v) if e is not None else 0.0)
            if xs:
                any_bar = True
                ax.bar(xs, ys, w * 0.92, color=col[n], label=_label(n),
                       yerr=[[0.0] * len(es), es] if err_key else None, capsize=2, error_kw={"lw": 0.8})
        ax.set_xticks(range(len(apps)))
        ax.set_xticklabels([app_l(a) for a in apps], fontsize=9)
        ax.set_title(title, fontsize=10)
        ax.set_ylabel(ylabel, fontsize=9)
        ax.grid(axis="y", alpha=0.3)
        if log and any_bar:
            ax.set_yscale("log")
        if any_bar:
            lo, hi = ax.get_ylim()
            ax.set_ylim(top=hi * 4 if log else hi * 1.25)      # room for the legend
            ax.legend(fontsize=8)
        else:
            ax.text(0.5, 0.5, T("没有数据", "no data"), ha="center", va="center", transform=ax.transAxes, color="#888888")

    def fg_bg(ax, rows, fg_key, bg_key, title, ylabel, log=False, fg_err=None, bg_err=None):
        """x = app, per run two bars: foreground (hatched, light) and background (solid)."""
        apps = list(dict.fromkeys(r["app"] for r in rows))
        w = 0.8 / max(1, 2 * len(names))
        any_bar = False
        for i, n in enumerate(names):
            by = {r["app"]: r for r in rows if r["run"] == n}
            for k, (key, ek) in enumerate(((fg_key, fg_err), (bg_key, bg_err))):
                xs, ys, es = [], [], []
                for j, a in enumerate(apps):
                    v = (by.get(a) or {}).get(key)
                    if v is None:
                        continue
                    xs.append(j + (2 * i + k - (2 * len(names) - 1) / 2) * w)
                    ys.append(v)
                    e = (by.get(a) or {}).get(ek) if ek else None
                    es.append(max(0.0, e - v) if e is not None else 0.0)
                if xs:
                    any_bar = True
                    ax.bar(xs, ys, w * 0.92, color=col[n], alpha=0.45 if k == 0 else 1.0, hatch="//" if k == 0 else None,
                           edgecolor="white" if k == 0 else None, linewidth=0,
                           label=f"{_label(n)} · {T('前台', 'fg') if k == 0 else T('后台', 'bg')}",
                           yerr=[[0.0] * len(es), es] if ek else None, capsize=2, error_kw={"lw": 0.8})
        ax.set_xticks(range(len(apps)))
        ax.set_xticklabels([app_l(a) for a in apps], fontsize=9)
        ax.set_title(title, fontsize=10)
        ax.set_ylabel(ylabel, fontsize=9)
        ax.grid(axis="y", alpha=0.3)
        if log and any_bar:
            ax.set_yscale("log")
        if any_bar:
            lo, hi = ax.get_ylim()
            ax.set_ylim(top=hi * 6 if log else hi * 1.3)       # room for the legend
            ax.legend(fontsize=7, ncol=max(1, len(names)))
        else:
            ax.text(0.5, 0.5, T("没有数据", "no data"), ha="center", va="center", transform=ax.transAxes, color="#888888")

    grouped(axes[0][0], tb["interactive"], "resp_p50_ms", "resp_p95_ms",
            T("① 呼叫 / 切换响应时间 (柱 = P50, 须 = P95)", "1. call / switch response (bar P50, whisker P95)"), "ms")
    smooth_rows = [dict(r, smooth_ms=r["probe_p95_ms"] if r["probe_p95_ms"] is not None else r["echo_p50_ms"],
                        smooth_hi=r["probe_max_ms"] if r["probe_p95_ms"] is not None else r["echo_max_ms"])
                   for r in tb["interactive"] if r["app"] != "ALL"]
    grouped(axes[0][1], smooth_rows, "smooth_ms", "smooth_hi",
            T("① 切回后是否顺滑: 事件循环应答 P95 (不应答 ping 的应用用输入回读 P50), 须 = 最大",
              "1. smoothness after the switch: event-loop ping P95 (or input read-back P50), whisker = max"), "ms", log=True)
    fg_bg(axes[1][0], tb["compute"], "fg_job_p50_ms", "bg_job_p50_ms",
          T("② 计算型: 单个任务耗时 (柱 = P50, 须 = P95)", "2. compute: time per job (bar P50, whisker P95)"), "ms", log=True,
          fg_err="fg_job_p95_ms", bg_err="bg_job_p95_ms")
    fg_bg(axes[1][1], tb["compute"], "fg_jobs_per_min", "bg_jobs_per_min",
          T("② 计算型: 每分钟完成的任务数", "2. compute: jobs per minute"), T("任务 / 分钟", "jobs / min"))
    fg_bg(axes[2][0], tb["download"], "fg_mbps", "bg_mbps", T("③ 下载型: 下载速度", "3. download: speed"), "MB/s")
    rtt_rows = []
    for r in tb["download"]:
        rtt_rows.append(dict(r, app=T("建连: 到下载源", "connect: source")))
        if r.get("fg_ext_rtt_p50_ms") is not None or r.get("bg_ext_rtt_p50_ms") is not None:
            rtt_rows.append({"run": r["run"], "app": T("建连: 到外网", "connect: internet"), "fg_rtt_p50_ms": r["fg_ext_rtt_p50_ms"],
                             "bg_rtt_p50_ms": r["bg_ext_rtt_p50_ms"], "fg_rtt_p95_ms": r["fg_ext_rtt_p95_ms"],
                             "bg_rtt_p95_ms": r["bg_ext_rtt_p95_ms"]})
        # a log axis cannot show 0: an app that is never held back wakes up "0.0x ms" late
        rtt_rows.append({"run": r["run"], "app": T("处理延迟 (等 CPU)", "wake-up delay"),
                         "fg_rtt_p50_ms": max(r["fg_wake_p50_ms"], 0.01) if r.get("fg_wake_p50_ms") is not None else None,
                         "bg_rtt_p50_ms": max(r["bg_wake_p50_ms"], 0.01) if r.get("bg_wake_p50_ms") is not None else None,
                         "fg_rtt_p95_ms": r.get("fg_wake_p95_ms"), "bg_rtt_p95_ms": r.get("bg_wake_p95_ms")})
    fg_bg(axes[2][1], rtt_rows, "fg_rtt_p50_ms", "bg_rtt_p50_ms",
          T("③ 下载型: 网络时延 (TCP 建连) 与应用的处理延迟 (柱 = P50, 须 = P95)",
            "3. download: TCP connect time and wake-up delay (bar P50, whisker P95)"),
          "ms", log=True, fg_err="fg_rtt_p95_ms", bg_err="bg_rtt_p95_ms")
    fig.suptitle(T("三类应用: 不限制 vs 压制后台资源", "three app classes: unlimited vs limited background"), fontsize=12)
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return True


def plot_bench(run: dict, path: str) -> bool:
    """Per bench app, over the run: every job's time (compute) / MB per second and connect time
    (download); the stretches in which the app was in the foreground are shaded."""
    from .plots import _plt, _use_cjk_font
    cl = classes_of(run)
    apps = cl["compute"] + cl["download"]
    data = {a: bench_events(run, a) for a in apps}
    apps = [a for a in apps if data[a]]
    if not apps:
        return False
    plt = _plt()
    zh = _use_cjk_font()
    T = (lambda z, e: z) if zh else (lambda z, e: e)
    t_ref = min(s["t_dwell"] for s in run["steps"] if s.get("t_dwell") is not None)
    fig, axes = plt.subplots(len(apps), 1, figsize=(12, 2.9 * len(apps)), squeeze=False, sharex=True)
    for ax, app in zip(axes[:, 0], apps):
        ev = data[app]
        for iv in intervals(run, app):
            if iv["pos"] == "fg":
                ax.axvspan(iv["t0"] - t_ref, iv["t1"] - t_ref, color="#2ca02c", alpha=0.15, lw=0)
        name = _cn(app) if zh else app
        if app in cl["compute"]:
            jobs = [e for e in ev if e.get("k") == "job"]
            ax.scatter([e["T"] - t_ref for e in jobs], [e["ms"] for e in jobs], s=5, color="#1f77b4")
            ax.set_yscale("log")
            ax.set_ylabel(T("单个任务耗时 (ms)", "ms per job"), fontsize=9)
        else:
            dl = [e for e in ev if e.get("k") == "dl" and e.get("s")]
            ax.plot([e["T"] - t_ref for e in dl], [e["b"] / 2 ** 20 / e["s"] for e in dl], color="#1f77b4", lw=1)
            ax.set_ylabel("MB/s", fontsize=9, color="#1f77b4")
            ax.set_ylim(bottom=0)
            rtt = [e for e in ev if e.get("k") == "rtt" and e.get("to") == "dl" and e.get("ms") is not None]
            if rtt:
                ax2 = ax.twinx()
                ax2.scatter([e["T"] - t_ref for e in rtt], [e["ms"] + (e.get("late") or 0) for e in rtt], s=4,
                            color="#d62728", alpha=0.6)
                ax2.set_yscale("log")
                ax2.set_ylabel(T("建连时延 + 处理延迟 (ms)", "connect + wake-up delay (ms)"), fontsize=9, color="#d62728")
        ax.set_title(f"{name}  ({T('绿色底 = 它在前台', 'green = in the foreground')})", fontsize=10)
        ax.grid(alpha=0.3)
    axes[-1, 0].set_xlabel(T("时间 (s)", "time (s)"))
    fig.suptitle(f"{_name(run)}: {_limits(run) if zh else _policy(run)}", fontsize=11)
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return True


# ---------------------------------------------------------------------------- CLI
def write_all(runs: list[dict], out_dir: str, plots: bool = True, log=print) -> list[str]:
    os.makedirs(out_dir, exist_ok=True)
    tb = tables(runs)
    made = [write_csv(tb["interactive"], INTERACTIVE_COLS, os.path.join(out_dir, "classes_interactive.csv")),
            write_csv(tb["compute"], COMPUTE_COLS, os.path.join(out_dir, "classes_compute.csv")),
            write_csv(tb["download"], DOWNLOAD_COLS, os.path.join(out_dir, "classes_download.csv"))]
    p = os.path.join(out_dir, "classes_report.md")
    with open(p, "w") as f:
        f.write(report_md(runs, tb))
    made.append(p)
    try:
        made.append(write_xlsx(runs, os.path.join(out_dir, "classes_tables.xlsx"), tb))
    except ImportError:
        log("classes_tables.xlsx skipped: pip install openpyxl")
    if plots:
        try:
            pd = os.path.join(out_dir, "plots")
            os.makedirs(pd, exist_ok=True)
            p = os.path.join(pd, "classes.png")
            if plot_classes(runs, p, tb):
                made.append(p)
            for r in runs:
                p = os.path.join(pd, _name(r) + "_bench.png")
                if plot_bench(r, p):
                    made.append(p)
        except ImportError:
            log("plots skipped: pip install matplotlib")
    return made


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+")
    ap.add_argument("--out", metavar="DIR", help="write the CSVs, classes_report.md, classes_tables.xlsx and plots/ here")
    ap.add_argument("--no-plots", action="store_true")
    args = ap.parse_args(argv)
    runs = [r for r in (load(p) for p in args.files) if r["steps"]]
    if not runs:
        print("no steps in the given files", file=sys.stderr)
        return 1
    runs.sort(key=lambda r: (_policy(r) != "none", _name(r)))
    if args.out:
        for p in write_all(runs, args.out, plots=not args.no_plots):
            print("wrote", p)
    print(report_md(runs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
