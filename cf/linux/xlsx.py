"""Excel export of the squeeze-experiment tables (needs openpyxl; `pip install openpyxl`).

One workbook, four sheets, Chinese headers (the figures go straight into the thesis):

  统计表    per app, the aggressive cell next to the `none` baseline: active footprint m_fg, background
            resident PSS, % of m_fg, CPU obtained in the background (% of one core), throttled share,
            reclaim / swap-in per minute, switch-back P50 / P95
  三格对照  the same per-app figures for every run
  每格汇总  one row per run: the summary columns of cf.linux.analyze (squeeze view)
  每进程    per (app, process name): foreground vs background PSS, swap, squeeze %, swap growth

    python3 -m cf.linux.analyze results/arm64_squeeze/*.jsonl --xlsx results/arm64_squeeze/squeeze_tables.xlsx
"""
from __future__ import annotations

from .analyze import per_process_rows, squeeze_app_rows, summarize

APP_CN = {"wechat": "微信", "qq": "QQ", "dingtalk": "钉钉", "wps": "WPS", "firefox": "Firefox", "wemeet": "腾讯会议",
          "baidunetdisk": "百度网盘", "xunlei": "迅雷", "iqiyi": "爱奇艺", "bilibili": "哔哩哔哩", "netease_music": "网易云音乐",
          "zwcad": "中望CAD", "shimo": "石墨文档", "douyin": "抖音", "chrome": "Chrome", "feishu": "飞书", "wecom": "企业微信",
          "sevenzip": "7-Zip", "z3": "Z3 求解器",
          "bench_numeric": "科学计算", "bench_compress": "压缩解压", "bench_download": "下载"}

SUMMARY_KEYS = [("policy", "策略"), ("fg_pss_avg_mb", "前台 PSS 均值 (MB)"), ("bg_pss_quiet_avg_mb", "后台驻留 PSS 均值 (MB)"),
                ("bg_pct_of_m_fg", "后台驻留占 m_fg (%)"), ("bg_squeeze_pct", "bg_squeeze_pct (%)"),
                ("quiet_swap_out_mb_per_min", "静默期系统换出 (MB/min)"), ("quiet_bg_reclaim_mb_per_min", "静默期后台回收 (MB/min)"),
                ("quiet_bg_swap_growth_mb_per_min", "静默期后台 swap 净增 (MB/min)"), ("quiet_zram_growth_mb_per_min", "静默期 zram 物理增长 (MB/min)"),
                ("zram_phys_avg_mb", "zram 物理占用均值 (MB)"), ("quiet_s", "静默秒数"), ("swap_write_mb", "swap 写 (MB)"),
                ("swap_read_mb", "swap 读 (MB)"), ("refault_anon", "匿名页 refault"), ("lat_p50_ms", "切回 P50 (ms)"),
                ("lat_p95_ms", "切回 P95 (ms)"), ("ping_p95_ms", "ping P95 (ms)"), ("bg_cores_avg", "后台总 CPU (核)"),
                ("bg_throttled_pct", "后台被节流占比 (%)"), ("psi_cpu_some_ms", "CPU PSI some (ms)"), ("state_lost_events", "状态丢失"),
                ("oom_kills", "OOM"), ("n_cold", "冷启动"), ("work_checks", "内容核对次数"), ("work_fail", "内容核对失败")]

APP_KEYS = [("m_fg_mb", "m_fg (MB)"), ("bg_resid_mb", "后台驻留 (MB)"), ("bg_pct_of_m_fg", "驻留占 m_fg (%)"),
            ("bg_cpu_pct_core", "后台单核 CPU (%)"), ("bg_throttled_pct", "被节流占比 (%)"), ("bg_reclaim_mb_per_min", "每分钟回收 (MB)"),
            ("bg_swapin_mb_per_min", "每分钟页回 (MB)"), ("n_req", "被请求次数"), ("lat_p50_ms", "切回 P50 (ms)"),
            ("lat_p95_ms", "切回 P95 (ms)"), ("ping_p50_ms", "ping P50 (ms)")]

PROC_KEYS = [("proc", "进程名"), ("n_pids", "进程数"), ("pss_fg_mb", "前台 PSS (MB)"), ("pss_bg_mb", "刚切走 PSS (MB)"),
             ("pss_bg_quiet_mb", "后台停留结束 PSS (MB)"), ("swap_bg_quiet_mb", "那时在 swap 里 (MB)"), ("squeeze_pct", "被挤掉 (%)"),
             ("swap_growth_mb_per_min", "静默期 SwapPss 增长 (MB/min)")]

NOTES = ["说明: m_fg = warmup 时前台 PSS+Swap (活跃足迹); 后台驻留 = 该应用在后台时每次停留结束的驻留 PSS 平均; 单核 CPU 利用率 = "
         "cgroup CPU 时间 / 墙钟 (cpu_cores 0.1 即配额为单核 10 %); 被节流时间占比 = cgroup throttled_usec / 墙钟;",
         "每分钟回收 = memory.stat pgsteal 增量 (匿名页换出 + 文件页丢弃); 每分钟页回 = major fault × 4 KB; 切回 = 该应用被请求回前台的"
         "总耗时 (激活 + ping); '未被请求' = 本次轨迹没有抽到它."]


def _name(run: dict) -> str:
    cfg = run["meta"].get("config", {})
    return cfg.get("name") or str(run.get("path", "run"))


def _cn(app: str) -> str:
    return APP_CN.get(app, app)


def _lat(r: dict, key: str):
    return r.get(key) if r.get("n_req") else "未被请求"


def write_xlsx(runs: list[dict], path: str, main: str | None = None, baseline: str | None = None) -> str:
    """`main` / `baseline` are run names for the 统计表 sheet; by default the first `static`
    (or non-`none`) run and the first `none` run."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    names = [_name(r) for r in runs]
    by_name = dict(zip(names, runs))
    pol = {n: (r["meta"].get("config", {}).get("policy") or "") for n, r in by_name.items()}
    baseline = baseline or next((n for n in names if pol[n] == "none"), None)
    main = main or next((n for n in names if pol[n] != "none"), names[0])
    sa = {n: {r["app"]: r for r in squeeze_app_rows(r_)} for n, r_ in by_name.items()}
    apps = list(by_name[main]["meta"].get("apps") or [])

    bold = Font(bold=True)
    hdr_fill, sub_fill = PatternFill("solid", fgColor="DDEBF7"), PatternFill("solid", fgColor="F2F2F2")
    thin = Side(style="thin", color="BBBBBB")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    def style(ws, freeze: str):
        for row in ws.iter_rows():
            for c in row:
                c.border = border
                c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        for c in ws[1]:
            c.font = bold
            c.fill = hdr_fill
        for col in ws.columns:
            w = max(len(str(c.value)) if c.value is not None else 0 for c in col)
            ws.column_dimensions[get_column_letter(col[0].column)].width = min(max(10, w * 1.1 + 2), 40)
        ws.row_dimensions[1].height = 45
        ws.freeze_panes = freeze

    wb = Workbook()
    ws = wb.active
    ws.title = "统计表"
    b = baseline is not None
    head = ["应用", "活跃足迹 m_fg (MB)", f"{main} 后台驻留 (MB)"] + ([f"{baseline} 后台驻留 (MB)"] if b else []) \
        + ["驻留占 m_fg (%)", f"{main} 后台单核 CPU 利用率 (%)"] + ([f"{baseline} 后台单核 CPU (%)"] if b else []) \
        + ["被节流时间占比 (%)", "每分钟回收 (MB/min)", "每分钟页回 (MB/min)", "切回 P50 (ms)", "切回 P95 (ms)"] \
        + ([f"{baseline} 切回 P50 (ms)", f"{baseline} 切回 P95 (ms)"] if b else [])
    ws.append(head)
    tot = {"m": 0.0, "s": 0.0, "n": 0.0, "rc": 0.0, "si": 0.0}
    for a in apps:
        s = sa[main].get(a) or {}
        n = (sa[baseline].get(a) if b else None) or {}
        ws.append([_cn(a), s.get("m_fg_mb"), s.get("bg_resid_mb")] + ([n.get("bg_resid_mb")] if b else [])
                  + [s.get("bg_pct_of_m_fg"), s.get("bg_cpu_pct_core")] + ([n.get("bg_cpu_pct_core")] if b else [])
                  + [s.get("bg_throttled_pct"), s.get("bg_reclaim_mb_per_min"), s.get("bg_swapin_mb_per_min"),
                     _lat(s, "lat_p50_ms"), _lat(s, "lat_p95_ms")]
                  + ([_lat(n, "lat_p50_ms"), _lat(n, "lat_p95_ms")] if b else []))
        tot["m"] += s.get("m_fg_mb") or 0
        tot["s"] += s.get("bg_resid_mb") or 0
        tot["n"] += n.get("bg_resid_mb") or 0
        tot["rc"] += s.get("bg_reclaim_mb_per_min") or 0
        tot["si"] += s.get("bg_swapin_mb_per_min") or 0
    cpus = [s.get("bg_cpu_pct_core") for s in sa[main].values() if s.get("bg_cpu_pct_core") is not None]
    cap = by_name[main]["meta"].get("config", {}).get("throttle", {}).get("cpu_cores")
    ws.append(["合计 / 均值", round(tot["m"]), round(tot["s"])] + ([round(tot["n"])] if b else [])
              + [round(100 * tot["s"] / tot["m"], 1) if tot["m"] else None,
                 (f"上限 {100 * cap:g}, " if cap else "") + (f"实际 {min(cpus):g}–{max(cpus):g}" if cpus else "")]
              + ([""] if b else []) + ["", round(tot["rc"], 1), round(tot["si"], 1), "", ""] + (["", ""] if b else []))
    for c in ws[ws.max_row]:
        c.font = bold
        c.fill = sub_fill
    style(ws, "B2")
    ws.append([])
    cfg = by_name[main]["meta"].get("config", {})
    thr = cfg.get("throttle", {})
    notes = NOTES + [f"数据: {main}" + (f" vs {baseline}" if b else "") + f"; 场景 {cfg.get('trace', {}).get('scenario')}, "
                     f"T={cfg.get('trace', {}).get('T')}, 停留 {cfg.get('dwell_s')} s; 限流: cpu_mode {thr.get('cpu_mode')}, cpu_cores "
                     f"{thr.get('cpu_cores')}, cpu_weight {thr.get('cpu_weight')}, mem_frac {thr.get('mem_frac')}, mem_min_mb "
                     f"{thr.get('mem_min_mb')}, squeeze_rounds {thr.get('squeeze_rounds')}, squeeze_every_s {thr.get('squeeze_every_s')}, "
                     f"thrash_high_per_s {thr.get('thrash_high_per_s')}."]
    for note in notes:
        ws.append([note])
        r = ws.max_row
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=len(head))
        ws.cell(r, 1).alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
        ws.row_dimensions[r].height = 32

    ws2 = wb.create_sheet("三格对照" if len(runs) == 3 else "各格对照")
    ws2.append(["格", "应用"] + [cn for _, cn in APP_KEYS])
    for n in names:
        for a in apps:
            r = sa[n].get(a) or {}
            ws2.append([n, _cn(a)] + [r.get(k) for k, _ in APP_KEYS])
    style(ws2, "C2")

    ws3 = wb.create_sheet("每格汇总")
    ws3.append(["格"] + [cn for _, cn in SUMMARY_KEYS])
    for n in names:
        r = summarize(by_name[n])
        ws3.append([n] + [r.get(k) for k, _ in SUMMARY_KEYS])
    style(ws3, "B2")

    ws4 = wb.create_sheet("每进程")
    ws4.append(["格", "应用"] + [cn for _, cn in PROC_KEYS])
    for n in names:
        for r in per_process_rows(by_name[n]):
            ws4.append([n, _cn(r["app"])] + [r.get(k) for k, _ in PROC_KEYS])
    style(ws4, "D2")

    wb.save(path)
    return path
