"""Figures for the Ubuntu throttling runs (matplotlib, Agg backend).

  <run>.png            one figure per run: background memory vs budget, switch latency per step
                       (coloured by where the app came from: fg / bg / shrunk), CPU the background got
                       and how much of the time it was throttled, swap traffic + downloads + notifications
  <run>_timeline.png   when sample_every_s > 0: the within-dwell time series (bg memory per app, cores,
                       PSI, swap)
  latency_cdf.png      CDF of switch latency per policy (all runs), split resident / shrunk
  bg_tradeoff.png      per run: background PSS saving vs P95 latency, marker = policy, colour = scenario
  <run>_swapflow.png   per app, from the moment it was switched away: memory the kernel reclaimed from its
                       cgroup (out) vs memory it paged back (in), per sample interval and cumulative, every
                       background episode overlaid + mean - shows the transient after the switch and whether
                       the two flows converge (steady state)
  squeeze_apps.png     runs recorded with per_process: per app, grouped by run - active footprint vs
                       background resident PSS, CPU in the background (% of a core), swap traffic per
                       minute (reclaimed / paged back), switch-back latency

    python3 -m cf.linux.analyze results/matrix/*.jsonl --plots-dir results/matrix/plots
"""
from __future__ import annotations

import os
from collections import defaultdict
from typing import Optional

SRC_COLOR = {"fg": "#9e9e9e", "bg": "#1f77b4", "shr": "#d62728", "cold": "#000000"}


def _plt():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    return plt


def _name(run: dict) -> str:
    cfg = run["meta"].get("config", {})
    return cfg.get("name") or os.path.basename(run.get("path", "run")).replace(".jsonl", "")


def _src(s: dict) -> str:
    if s["launch"].get("launch_state") == "COLD":
        return "cold"
    if s.get("was_shrunk"):
        return "shr"
    if s.get("was_throttled"):
        return "bg"
    return "fg"


def plot_run(run: dict, path: str) -> bool:
    plt = _plt()
    steps = run["steps"]
    if not steps:
        return False
    cats = run["meta"].get("categories") or {}
    t = [s["t"] + 1 for s in steps]
    budget = [s.get("budget_kb", 0) / 1024 for s in steps]
    mbg = [s.get("M_bg_kb", 0) / 1024 for s in steps]
    lat = [s["launch"].get("total_time_ms") for s in steps]
    src = [_src(s) for s in steps]
    cores, thr, swap_w, swap_r, dl, ntf = [], [], [], [], [], []
    for s in steps:
        others = {p: b for p, b in (s.get("bg") or {}).items() if p != s["req"]}
        cores.append(sum(b.get("cores") or 0 for b in others.values()))
        den = sum(b.get("wall_s") or 0 for b in others.values() if b.get("tier") != "fg")
        num = sum(b.get("throttled_ms", 0) / 1000 for b in others.values() if b.get("tier") != "fg")
        thr.append(100 * num / den if den else 0)
        dl.append(sum(b.get("write_mb", 0) for p, b in others.items() if cats.get(p) == "download"))
        ntf.append(sum(b.get("notifications", 0) for b in others.values()))
        vm1 = s["after_launch"]["system"].get("vmstat") or {}
        vm2 = s["after_dwell"]["system"].get("vmstat") or {}
        swap_w.append(max(0, (vm2.get("pswpout", 0) - vm1.get("pswpout", 0))) * 4 / 1024)
        swap_r.append(max(0, (vm2.get("pswpin", 0) - vm1.get("pswpin", 0))) * 4 / 1024)

    fig, ax = plt.subplots(4, 1, figsize=(11, 11), sharex=True)
    fig.suptitle(_name(run))
    ax[0].plot(t, mbg, label="background PSS+swap (MB)")
    ax[0].plot(t, budget, "--", label="budget B (MB)")
    ax[0].set_ylabel("MB"); ax[0].legend(loc="upper right"); ax[0].grid(alpha=.3)
    for k, lab in (("fg", "from fg/FRONT"), ("bg", "from bg (CPU-limited)"), ("shr", "from shrunk (memory.high)"),
                   ("cold", "COLD (restarted!)")):
        xs = [x for x, v, c in zip(t, lat, src) if c == k and v is not None]
        ys = [v for v, c in zip(lat, src) if c == k and v is not None]
        if xs:
            ax[1].scatter(xs, ys, s=18, color=SRC_COLOR[k], label=lab)
    ax[1].set_ylabel("switch latency (ms)"); ax[1].set_yscale("symlog", linthresh=100); ax[1].legend(loc="upper right", fontsize=8)
    ax[1].grid(alpha=.3)
    ax[2].plot(t, cores, label="background cores (sum)")
    ax2 = ax[2].twinx(); ax2.plot(t, thr, color="#d62728", alpha=.6, label="throttled %"); ax2.set_ylabel("%")
    ax[2].set_ylabel("cores"); ax[2].grid(alpha=.3)
    h1, l1 = ax[2].get_legend_handles_labels(); h2, l2 = ax2.get_legend_handles_labels()
    ax[2].legend(h1 + h2, l1 + l2, loc="upper right", fontsize=8)
    ax[3].bar(t, swap_w, width=.8, label="swap out (MB)", color="#ff7f0e", alpha=.7)
    ax[3].bar(t, [-x for x in swap_r], width=.8, label="swap in (MB)", color="#2ca02c", alpha=.7)
    ax3 = ax[3].twinx(); ax3.plot(t, dl, "k.-", ms=3, label="download MB / step"); ax3.plot(t, ntf, "m.", label="notifications")
    ax[3].set_ylabel("MB"); ax[3].set_xlabel("step"); ax[3].grid(alpha=.3)
    h1, l1 = ax[3].get_legend_handles_labels(); h2, l2 = ax3.get_legend_handles_labels()
    ax[3].legend(h1 + h2, l1 + l2, loc="upper right", fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return True


def plot_timeline(run: dict, path: str) -> bool:
    """Within-dwell series (needs sample_every_s > 0)."""
    plt = _plt()
    steps = [s for s in run["steps"] if s.get("timeline")]
    if not steps:
        return False
    apps = run["meta"].get("apps") or []
    xs, mem, cores_t, psi, swapfree = [], defaultdict(list), [], [], []
    clock = 0.0
    for s in steps:
        base = clock
        prev = None
        for pt in s["timeline"]:
            x = base + pt["dt"]
            xs.append(x)
            for a in apps:
                d = (pt["apps"] or {}).get(a) or {}
                mem[a].append((d.get("cg_mem_mb") or 0) if a != s["req"] else 0)
            if prev is not None:
                du = sum((pt["apps"][a].get("cg_usage_ms") or 0) - (prev["apps"][a].get("cg_usage_ms") or 0)
                         for a in apps if a != s["req"] and a in pt["apps"] and a in prev["apps"])
                cores_t.append(du / 1000 / max(0.01, pt["dt"] - prev["dt"]))
            else:
                cores_t.append(0)
            psi.append(((pt["system"].get("psi_cpu") or {}).get("some_avg10")) or 0)
            swapfree.append((pt["system"].get("swap_free_kb") or 0) / 1024)
            prev = pt
        clock = base + (s.get("dwell_s") or s.get("step_s") or 0)
    fig, ax = plt.subplots(3, 1, figsize=(12, 8), sharex=True)
    fig.suptitle(_name(run) + " - within-dwell samples")
    bottom = [0.0] * len(xs)
    for a in apps:
        ax[0].fill_between(xs, bottom, [b + m for b, m in zip(bottom, mem[a])], step="post", alpha=.7, label=a)
        bottom = [b + m for b, m in zip(bottom, mem[a])]
    ax[0].set_ylabel("background cgroup memory (MB)"); ax[0].legend(fontsize=7, ncol=4, loc="upper right")
    ax[1].plot(xs, cores_t, lw=.8); ax[1].set_ylabel("background cores"); ax[1].grid(alpha=.3)
    ax[2].plot(xs, psi, label="PSI cpu some avg10 (%)"); ax2 = ax[2].twinx(); ax2.plot(xs, swapfree, color="#ff7f0e", label="SwapFree (MB)")
    ax[2].set_xlabel("s (dwell time, concatenated)"); ax[2].grid(alpha=.3)
    h1, l1 = ax[2].get_legend_handles_labels(); h2, l2 = ax2.get_legend_handles_labels()
    ax[2].legend(h1 + h2, l1 + l2, fontsize=8, loc="upper right")
    fig.tight_layout(); fig.savefig(path, dpi=110); plt.close(fig)
    return True


def plot_latency_cdf(runs: list[dict], path: str) -> bool:
    plt = _plt()
    by_pol: dict[str, dict[str, list[float]]] = defaultdict(lambda: {"resident": [], "shrunk": []})
    for r in runs:
        pol = r["meta"].get("config", {}).get("policy", "?")
        for s in r["steps"]:
            v = s["launch"].get("total_time_ms")
            if v is None or s["launch"].get("launch_state") in ("FRONT", "COLD"):
                continue
            by_pol[pol]["shrunk" if s.get("was_shrunk") else "resident"].append(v)
    if not by_pol:
        return False
    fig, ax = plt.subplots(1, 2, figsize=(11, 4), sharey=True)
    for i, key in enumerate(("resident", "shrunk")):
        for pol, d in sorted(by_pol.items()):
            v = sorted(d[key])
            if not v:
                continue
            ax[i].plot(v, [(k + 1) / len(v) for k in range(len(v))], label=f"{pol} (n={len(v)})")
        ax[i].set_title(f"switch latency, app came from {key}")
        ax[i].set_xscale("log"); ax[i].set_xlabel("ms"); ax[i].grid(alpha=.3); ax[i].legend(fontsize=8)
    ax[0].set_ylabel("CDF")
    fig.tight_layout(); fig.savefig(path, dpi=110); plt.close(fig)
    return True


def plot_tradeoff(rows: list[dict], path: str) -> bool:
    plt = _plt()
    # saving relative to the `none` baseline of the same scenario (and seed when present); when a scenario
    # has no baseline run, fall back to the sum of foreground sizes
    base = {}
    for r in rows:
        if r.get("policy") == "none" and r.get("bg_pss_avg_mb"):
            base[(str(r.get("scenario")), r.get("seed"))] = r["bg_pss_avg_mb"]
            base.setdefault((str(r.get("scenario")), None), r["bg_pss_avg_mb"])
    pts, vs_none = [], False
    for r in rows:
        if r.get("bg_pss_avg_mb") is None or r.get("lat_p95_ms") is None:
            continue
        ref = base.get((str(r.get("scenario")), r.get("seed"))) or base.get((str(r.get("scenario")), None))
        if ref:
            vs_none = True
        elif not r.get("sum_m_fg_mb"):
            continue
        r = dict(r)
        r["mem_saving_pct"] = 100 * (1 - r["bg_pss_avg_mb"] / (ref or r["sum_m_fg_mb"]))
        pts.append(r)
    if not pts:
        return False
    markers = {"none": "x", "static": "s", "lru": "o", "landlord": "^", "hybrid": "D", "markov": "v", "belady": "*"}
    scen = sorted({str(r.get("scenario")) for r in pts})
    cmap = plt.get_cmap("tab10")
    fig, ax = plt.subplots(figsize=(8, 5.5))
    for r in pts:
        ax.scatter(r["mem_saving_pct"], r["lat_p95_ms"], marker=markers.get(r["policy"], "o"),
                   color=cmap(scen.index(str(r.get("scenario"))) % 10), s=50, alpha=.85)
        ax.annotate(f"{r['policy']} η={r['eta']}", (r["mem_saving_pct"], r["lat_p95_ms"]), fontsize=6, alpha=.7)
    for p, m in markers.items():
        if any(r["policy"] == p for r in pts):
            ax.scatter([], [], marker=m, color="k", label=p)
    for i, sc in enumerate(scen):
        ax.scatter([], [], marker="o", color=cmap(i % 10), label=f"scenario {sc}")
    ax.set_xlabel("background memory saving vs " + ("the none baseline (%)" if vs_none else "Σ m_fg (%)"))
    ax.set_ylabel("switch latency P95 (ms)")
    ax.set_yscale("log"); ax.grid(alpha=.3); ax.legend(fontsize=7, ncol=2)
    fig.tight_layout(); fig.savefig(path, dpi=110); plt.close(fig)
    return True


def swap_flow_episodes(run: dict, app: str) -> list[dict]:
    """Background episodes of `app`: consecutive steps in which it was not the requested app, starting
    right after a step in which it was (from_start=False) or at the beginning of the run (True).
    Each episode is {"from_start", "pts": [(t_s since the switch-away, reclaimed_mb, paged_back_mb,
    swap_delta_mb)]} cumulative from the episode start, taken from after_launch (t=0), the dwell
    samples and after_dwell of every step."""
    steps = run["steps"]
    out: list[dict] = []
    cur: list[tuple[float, float, float, float]] = []
    base = None
    t_off = 0.0
    from_start = True

    def pt(d: dict, t: float):
        nonlocal base
        ps, mf, sw = d.get("cg_pgsteal"), d.get("majflt"), d.get("cg_swap_mb")
        if ps is None or mf is None:
            return None
        if base is None:
            base = (ps, mf, sw or 0.0)
        return (round(t, 2), max(0.0, (ps - base[0]) * 4 / 1024), max(0.0, (mf - base[1]) * 4 / 1024),
                (sw or 0.0) - base[2])

    for s in steps:
        if s["req"] == app:
            if cur:
                out.append({"from_start": from_start, "pts": cur})
            cur, base, t_off, from_start = [], None, 0.0, False
            continue
        d1 = (s.get("after_launch") or {}).get("apps", {}).get(app)
        if not d1 or d1.get("cg_pgsteal") is None:
            continue
        if not cur:
            p0 = pt(d1, 0.0)
            if p0:
                cur.append(p0)
        for x in s.get("timeline") or []:
            d = (x.get("apps") or {}).get(app)
            if d:
                p = pt(d, t_off + (x.get("dt") or 0))
                if p:
                    cur.append(p)
        d2 = (s.get("after_dwell") or {}).get("apps", {}).get(app)
        if d2:
            p = pt(d2, t_off + (s.get("dwell_s") or 0))
            if p:
                cur.append(p)
        t_off += (s.get("dwell_s") or 0) + 3.0          # ~switch overhead between steps
    if cur:
        out.append({"from_start": from_start, "pts": cur})
    return out


def _slope_mb_per_min(pts, idx: int, frac: float = 0.5):
    """Slope of column idx over the last `frac` of the episode (MB/min)."""
    if len(pts) < 3:
        return None
    t_end = pts[-1][0]
    tail = [p for p in pts if p[0] >= t_end * (1 - frac)]
    if len(tail) < 2 or tail[-1][0] <= tail[0][0]:
        return None
    return 60 * (tail[-1][idx] - tail[0][idx]) / (tail[-1][0] - tail[0][0])


def _transient_end_s(pts) -> float | None:
    """Seconds after the switch-away until the per-interval reclaim settles: the first interval whose
    reclaimed MB is at most twice the median of the second half of the episode."""
    if len(pts) < 6:
        return None
    d = [(pts[k][0], pts[k][1] - pts[k - 1][1]) for k in range(1, len(pts))]
    tail = sorted(v for _, v in d[len(d) // 2:])
    med = tail[len(tail) // 2]
    for t, v in d:
        if v <= 2 * med + 1.0:
            return t
    return None


_CJK_FONTS = ("Noto Sans CJK SC", "Noto Sans SC", "Source Han Sans SC", "WenQuanYi Micro Hei", "WenQuanYi Zen Hei",
              "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "SimHei", "Noto Sans CJK JP", "Noto Sans CJK TC",
              "Noto Sans CJK HK", "Noto Sans CJK KR", "Noto Serif CJK SC", "Noto Serif CJK JP", "AR PL UMing CN",
              "AR PL UKai CN", "Droid Sans Fallback")
_LATIN_FONT = "DejaVu Sans"            # ships with matplotlib
_CJK_PROBE = "中文应用压缩解压时延前后台"
_LATIN_PROBE = "AZaz09:/()=%.,-+"


def _covers(fname: str, text: str) -> bool:
    try:
        from matplotlib.ft2font import FT2Font
        cmap = FT2Font(fname).get_charmap()
    except Exception:    # noqa: BLE001 - an unreadable font file is simply not a candidate
        return False
    return all(ord(c) in cmap for c in text)


def _add_new_fonts(font_manager) -> int:
    """matplotlib lists the fonts once and keeps the list in ~/.cache/matplotlib: a font installed
    afterwards (`apt install fonts-wqy-microhei` because the figures had boxes) stays invisible to
    it.  Register the font files it does not know yet -> how many were added."""
    try:
        known = {os.path.realpath(f.fname) for f in font_manager.fontManager.ttflist}
        new = [p for p in font_manager.findSystemFonts() if os.path.realpath(p) not in known]
    except Exception:    # noqa: BLE001
        return 0
    n = 0
    for path in new:
        try:
            font_manager.fontManager.addfont(path)
            n += 1
        except Exception:    # noqa: BLE001 - a format FreeType cannot read
            pass
    return n


def _pick_cjk_font(fonts: Optional[list] = None) -> tuple:
    """(family, has Latin too) of the Chinese font to draw with, ("", False) when none is installed.
    `fonts`: [(family, file)], default every font matplotlib knows.  A font that also has letters and
    digits wins over one that has only Chinese glyphs (Droid Sans Fallback: often the only CJK font
    of a minimal install - drawn alone it turns every digit and letter into an empty box);
    $CF_PLOT_FONT names a family to use regardless of the preference order."""
    if fonts is None:
        from matplotlib import font_manager
        _add_new_fonts(font_manager)
        fonts = [(f.name, f.fname) for f in font_manager.fontManager.ttflist]
    files: dict = {}
    for name, fname in fonts:
        files.setdefault(name, fname)
    forced = os.environ.get("CF_PLOT_FONT", "")
    listed = ([forced] if forced in files else []) + [n for n in _CJK_FONTS if n in files]
    rest = sorted(n for n in files if n not in listed and "Mono" not in n)
    cjk_only = ""
    for names in (listed, rest):
        for name in names:
            if not _covers(files[name], _CJK_PROBE):
                continue
            if name == forced or _covers(files[name], _LATIN_PROBE):
                return name, _covers(files[name], _LATIN_PROBE)
            cjk_only = cjk_only or name
    return cjk_only, False


_font_choice: Optional[tuple] = None


def _use_cjk_font() -> bool:
    """Make matplotlib draw Chinese text; False when it cannot (the figures then fall back to
    English labels instead of empty boxes).  font.family becomes a list, which matplotlib >= 3.6
    walks per glyph: [Chinese font, DejaVu Sans] when the Chinese font has Latin glyphs too, and
    [DejaVu Sans, Chinese font] when it has not - letters, digits and the math text of log axes
    then come from DejaVu Sans, Chinese from the other one.  (Putting the font first in
    font.sans-serif is not enough: only the first match of a generic family is ever used.)"""
    global _font_choice
    from matplotlib import font_manager, rcParams
    if _font_choice is None or os.environ.get("CF_PLOT_FONT", "") != _font_choice[2]:
        _font_choice = (*_pick_cjk_font(), os.environ.get("CF_PLOT_FONT", ""))
    name, latin, _ = _font_choice
    if not name:
        return False
    if latin:
        rcParams["font.family"] = [name, _LATIN_FONT]
    elif hasattr(font_manager.FontManager, "_find_fonts_by_props"):      # per-glyph fallback, matplotlib >= 3.6
        rcParams["font.family"] = [_LATIN_FONT, name]
    else:
        return False
    rcParams["axes.unicode_minus"] = False
    return True


def _app_cn(app: str) -> str:
    from .xlsx import APP_CN
    return APP_CN.get(app, app)


def plot_swap_flow(run: dict, path: str) -> bool:
    """Per app: how fast its memory is pushed out (reclaimed from the cgroup) and pulled back in
    (major faults) after it was switched to the background - MB per sample interval on the left,
    cumulative on the right, every background episode overlaid on one time axis from the moment of
    the switch-away, mean in bold.  The transient right after the switch is shaded; the steady-state
    rates of the longest episode are annotated.  Labels are Chinese when a CJK font is installed."""
    apps = list(run["meta"].get("apps") or [])
    eps: dict[str, list[dict]] = {}
    for a in apps:
        all_eps = [e for e in swap_flow_episodes(run, a) if len(e["pts"]) >= 3]
        real = [e for e in all_eps if not e["from_start"]]
        if real or all_eps:
            eps[a] = real or all_eps          # never switched away in this sweep: show the run-start episode
    if not eps:
        return False
    plt = _plt()
    zh = _use_cjk_font()
    T = (lambda z, e: z) if zh else (lambda z, e: e)
    every = float(run["meta"].get("config", {}).get("sample_every_s") or 0) or 10.0
    c_out, c_in, c_sw = "#d62728", "#1f77b4", "#7f7f7f"
    nan = float("nan")
    fig, axes = plt.subplots(len(eps), 2, figsize=(13.5, 3.0 * len(eps)), squeeze=False)
    for i, (a, episodes) in enumerate(eps.items()):
        ax_f, ax_c = axes[i]
        name = _app_cn(a) if zh else a
        n_ep = len(episodes)
        longest = max(episodes, key=lambda e: e["pts"][-1][0])["pts"]
        nb = int(longest[-1][0] // every) + 1
        sum_out = [0.0] * nb; sum_in = [0.0] * nb; cnt = [0] * nb
        faint = 0.9 if n_ep == 1 else 0.25
        for e in episodes:
            pts = e["pts"]
            ts = [p[0] for p in pts]
            d_out, d_in, mid = [], [], []
            for k in range(1, len(pts)):
                dt = ts[k] - ts[k - 1]
                if dt <= 0 or dt > 2.5 * every:          # missing samples (interrupted step): break the line
                    d_out.append(nan); d_in.append(nan); mid.append((ts[k] + ts[k - 1]) / 2)
                    continue
                d_out.append((pts[k][1] - pts[k - 1][1]) * every / dt)
                d_in.append((pts[k][2] - pts[k - 1][2]) * every / dt)
                mid.append((ts[k] + ts[k - 1]) / 2)
            ax_f.plot(mid, d_out, color=c_out, alpha=faint, linewidth=0.8)
            ax_f.plot(mid, d_in, color=c_in, alpha=faint, linewidth=0.8)
            ax_c.plot(ts, [p[1] for p in pts], color=c_out, alpha=faint, linewidth=0.9)
            ax_c.plot(ts, [p[2] for p in pts], color=c_in, alpha=faint, linewidth=0.9)
            for m, o, v in zip(mid, d_out, d_in):
                if o != o:
                    continue
                bkt = min(nb - 1, int(m // every))
                sum_out[bkt] += o; sum_in[bkt] += v; cnt[bkt] += 1
        lbl_out = T("换出：内核从它那里收走的内存", "pushed out: memory the kernel reclaimed from it")
        lbl_in = T("换回：它自己又读回来的内存", "pulled back: memory it paged back in")
        if n_ep > 1:
            xs = [(bk + 0.5) * every for bk in range(nb) if cnt[bk]]
            ax_f.plot(xs, [sum_out[bk] / cnt[bk] for bk in range(nb) if cnt[bk]], color=c_out, linewidth=2.2,
                      label=lbl_out + T(f"（粗线 = {n_ep} 次的平均）", f" (bold = mean of {n_ep})"))
            ax_f.plot(xs, [sum_in[bk] / cnt[bk] for bk in range(nb) if cnt[bk]], color=c_in, linewidth=2.2,
                      label=lbl_in + T(f"（淡线 = 每次单独）", " (faint = each episode)"))
        else:
            ax_f.plot([], [], color=c_out, linewidth=2.2, label=lbl_out)
            ax_f.plot([], [], color=c_in, linewidth=2.2, label=lbl_in)
        ax_c.plot([], [], color=c_out, linewidth=2, label=T("累计换出", "cumulative out"))
        ax_c.plot([], [], color=c_in, linewidth=2, label=T("累计换回", "cumulative in"))
        ax_c.plot([p[0] for p in longest], [p[3] for p in longest], color=c_sw, linestyle="--", linewidth=1,
                  label=T("它占用的 swap 比切走时多了多少", "its swap usage vs the switch-away"))
        # first interval = the one-shot squeeze on the switch-away
        first_out = longest[1][1] - longest[0][1] if len(longest) > 1 else 0.0
        t_tr = _transient_end_s(longest) if len(longest) > 1 and longest[1][0] <= 2.5 * every else None
        if t_tr is not None and t_tr > 0:
            for ax in (ax_f, ax_c):
                ax.axvspan(0, t_tr, color="#ffd54f", alpha=0.25, linewidth=0)
            ax_f.text(t_tr, 0.97, T(f"  暂态 ≈ {t_tr:.0f} 秒\n  切走瞬间一次性挤出 {first_out:.0f} MB",
                                    f"  transient ≈ {t_tr:.0f} s\n  {first_out:.0f} MB pushed out at once"),
                      transform=ax_f.get_xaxis_transform(), ha="left", va="top", fontsize=8, color="#7a5c00")
        so, si = _slope_mb_per_min(longest, 1), _slope_mb_per_min(longest, 2)
        if so is not None and si is not None:
            ratio = (si / so * 100) if so > 1e-6 else 0.0
            if ratio >= 80:
                verdict = T("换出 ≈ 换回：达到稳态，内存只是在来回搬", "out ≈ in: steady state, pages just circulate")
            elif ratio >= 20:
                verdict = T("换出 > 换回：稳态，但一部分被丢掉的页不经缺页就回来了",
                            "out > in: steady, part of the dropped pages come back without faults")
            else:
                verdict = T("几乎不再换回：后台基本安静", "almost nothing comes back: the app is quiet")
            ax_c.text(0.98, 0.04, T(f"稳态（最长一次的后半段）：每分钟换出 {so:.0f} MB，换回 {si:.0f} MB（{ratio:.0f} %）\n{verdict}",
                                    f"steady state (last half of the longest episode): out {so:.0f}, in {si:.0f} MB/min ({ratio:.0f} %)\n{verdict}"),
                      transform=ax_c.transAxes, ha="right", va="bottom", fontsize=8,
                      bbox=dict(boxstyle="round", facecolor="white", alpha=0.85, edgecolor="#cccccc"))
        if episodes[0]["from_start"]:
            ep_txt = T("这轮里没被切走过，从实验开始就在后台", "never requested in this sweep: in the background from the start")
        else:
            ep_txt = T(f"被切到后台 {n_ep} 次", f"{n_ep} background episode{'s' if n_ep > 1 else ''}")
        ax_f.set_title(T(f"{name}：每 {every:g} 秒换出 / 换回多少（{ep_txt}）",
                         f"{a}: MB pushed out / pulled back per {every:g} s ({ep_txt})"), fontsize=10)
        ax_c.set_title(T(f"{name}：从切到后台起的累计量（两条线平行 = 稳态）",
                         f"{a}: cumulative since the switch-away (parallel lines = steady state)"), fontsize=10)
        for ax in (ax_f, ax_c):
            ax.grid(alpha=0.3); ax.set_xlim(left=0); ax.set_ylabel(T("内存量 (MB)", "MB"))
        ax_f.legend(fontsize=7, loc="upper right"); ax_c.legend(fontsize=7.5, loc="upper left")
        if i == len(eps) - 1:
            for ax in (ax_f, ax_c):
                ax.set_xlabel(T("切到后台后经过的时间 (秒)", "s since the app left the foreground"))
    fig.suptitle(T(f"{_name(run)}：应用切到后台后，内存被换出 / 换回的过程\n"
                   "红 = 换出（内核从该应用的 cgroup 回收的页，pgsteal）；蓝 = 换回（该应用重新缺页读回的页，major fault）；"
                   "黄色底 = 刚切走的暂态",
                   f"{_name(run)}: swap flow of the background apps\n"
                   "red = pushed out (pgsteal of the app's cgroup); blue = pulled back (major faults x 4 KB); "
                   "yellow = transient after the switch-away"), fontsize=11)
    fig.tight_layout(rect=(0, 0, 1, 0.97)); fig.savefig(path, dpi=110); plt.close(fig)
    return True


def plot_squeeze_apps(runs: list[dict], path: str) -> bool:
    """Five panels, one bar group per app, one bar per run (none / squeeze / mild ...)."""
    from .analyze import squeeze_app_rows
    runs = [r for r in runs if r["meta"].get("config", {}).get("per_process")]
    if not runs:
        return False
    plt = _plt()
    rows = {_name(r): squeeze_app_rows(r) for r in runs}
    apps = list(runs[0]["meta"].get("apps") or [])
    names = list(rows)
    cmap = plt.get_cmap("tab10")
    panels = [("bg_resid_mb", "resident PSS in the background (MB); hatched frame = active footprint m_fg", True),
              ("bg_pct_of_m_fg", "background resident as % of m_fg", False),
              ("bg_cpu_pct_core", "CPU obtained in the background (% of one core)", False),
              ("bg_reclaim_mb_per_min", "swap traffic per minute in the background (MB): solid = reclaimed from the cgroup, "
                                        "hatched = paged back (major faults)", False),
              ("lat_p95_ms", "switch-back latency when requested (ms): bar = P95, mark = P50; n/a = never requested", False)]
    fig, axes = plt.subplots(len(panels), 1, figsize=(max(8, 1.9 * len(apps) + 3), 3.1 * len(panels)))
    width = 0.8 / max(1, len(names))
    xs = list(range(len(apps)))
    for ax, (col, title, with_mfg) in zip(axes, panels):
        top = 0.0
        for j, name in enumerate(names):
            byapp = {r["app"]: r for r in rows[name]}
            x = [i + (j - (len(names) - 1) / 2) * width for i in xs]
            vals = [byapp.get(a, {}).get(col) for a in apps]
            if col == "bg_reclaim_mb_per_min":
                swin = [byapp.get(a, {}).get("bg_swapin_mb_per_min") or 0 for a in apps]
                ax.bar([xi - width / 4 for xi in x], [v or 0 for v in vals], width * 0.45, color=cmap(j % 10), label=name)
                ax.bar([xi + width / 4 for xi in x], swin, width * 0.45, color=cmap(j % 10), alpha=0.45, hatch="..")
                for xi, v, w in zip(x, vals, swin):
                    ax.text(xi - width / 4, v or 0, f"{v or 0:g}", ha="center", va="bottom", fontsize=6.5)
                    ax.text(xi + width / 4, w, f"{w:g}", ha="center", va="bottom", fontsize=6.5, color="#444444")
                top = max(top, max((v or 0) for v in vals), max(swin))
                continue
            ax.bar(x, [v or 0 for v in vals], width * 0.9, color=cmap(j % 10), label=name)
            if with_mfg and j == 0:
                mfg = [byapp.get(a, {}).get("m_fg_mb") or 0 for a in apps]
                ax.bar(xs, mfg, 0.8, fill=False, edgecolor="#555555", hatch="//", linewidth=0.8, label="m_fg")
                top = max(top, max(mfg))
            if col == "lat_p95_ms":
                p50 = [byapp.get(a, {}).get("lat_p50_ms") for a in apps]
                ax.plot([xi for xi, v in zip(x, p50) if v], [v for v in p50 if v], "k_", markersize=12, markeredgewidth=2)
                for xi, a in zip(x, apps):
                    if not byapp.get(a, {}).get("n_req"):
                        ax.text(xi, 0, "n/a", ha="center", va="bottom", fontsize=7, color="#777777")
            for xi, v in zip(x, vals):
                if v is not None:
                    ax.text(xi, v, f"{v:g}", ha="center", va="bottom", fontsize=7)
            top = max(top, max((v or 0) for v in vals))
        ax.set_xticks(xs); ax.set_xticklabels(apps)
        ax.set_title(title, fontsize=9.5); ax.grid(axis="y", alpha=0.3)
        if col == "bg_pct_of_m_fg":
            ax.axhline(10, color="#d62728", linestyle="--", linewidth=0.8)
            ax.text(-0.45, 10.5, "10 % target", color="#d62728", fontsize=8, va="bottom")
        if col == "bg_reclaim_mb_per_min" and top > 50:
            ax.set_yscale("symlog", linthresh=10); ax.set_ylim(0, top * 3)
        elif top > 0:
            ax.set_ylim(0, top * 1.18)
    h, l = axes[0].get_legend_handles_labels()
    fig.legend(h, l, loc="upper center", ncol=len(l), fontsize=8, bbox_to_anchor=(0.5, 0.985), frameon=False)
    fig.suptitle(f"per-app squeeze figures ({len(apps)} apps)", fontsize=11, y=0.998)
    fig.tight_layout(rect=(0, 0, 1, 0.975)); fig.savefig(path, dpi=110); plt.close(fig)
    return True


def plot_all(runs: list[dict], out_dir: str) -> list[str]:
    from .analyze import summarize
    os.makedirs(out_dir, exist_ok=True)
    made = []
    for r in runs:
        p = os.path.join(out_dir, _name(r) + ".png")
        if plot_run(r, p):
            made.append(p)
        p = os.path.join(out_dir, _name(r) + "_timeline.png")
        if plot_timeline(r, p):
            made.append(p)
        p = os.path.join(out_dir, _name(r) + "_swapflow.png")
        if plot_swap_flow(r, p):
            made.append(p)
    p = os.path.join(out_dir, "latency_cdf.png")
    if plot_latency_cdf(runs, p):
        made.append(p)
    p = os.path.join(out_dir, "bg_tradeoff.png")
    if plot_tradeoff([summarize(r) for r in runs], p):
        made.append(p)
    p = os.path.join(out_dir, "squeeze_apps.png")
    if plot_squeeze_apps(runs, p):
        made.append(p)
    return made
