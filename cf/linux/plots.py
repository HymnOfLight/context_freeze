"""Figures for the Ubuntu throttling runs (matplotlib, Agg backend).

  <run>.png            one figure per run: background memory vs budget, switch latency per step
                       (coloured by where the app came from: fg / bg / shrunk), CPU the background got
                       and how much of the time it was throttled, swap traffic + downloads + notifications
  <run>_timeline.png   when sample_every_s > 0: the within-dwell time series (bg memory per app, cores,
                       PSI, swap)
  latency_cdf.png      CDF of switch latency per policy (all runs), split resident / shrunk
  bg_tradeoff.png      per run: background PSS saving vs P95 latency, marker = policy, colour = scenario

    python3 -m cf.linux.analyze results/matrix/*.jsonl --plots-dir results/matrix/plots
"""
from __future__ import annotations

import os
from collections import defaultdict

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
    p = os.path.join(out_dir, "latency_cdf.png")
    if plot_latency_cdf(runs, p):
        made.append(p)
    p = os.path.join(out_dir, "bg_tradeoff.png")
    if plot_tradeoff([summarize(r) for r in runs], p):
        made.append(p)
    return made
