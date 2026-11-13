"""Download speed and network latency of real apps we cannot instrument (百度网盘, 迅雷 ...), read from
the kernel: `ss -tin` lists every TCP socket with its tcp_info, and each socket belongs to the
cgroup (`ss --cgroup`) / the processes (`ss -p`) of exactly one app.

Per app and sample interval (default 1 s) the sampler turns that into the same records the bundled
download bench writes, so cf.linux.classes treats both alike:

    {"t": epoch, "k": "dl",  "b": bytes received by the app's sockets, "s": seconds, "tx": bytes sent,
     "n": sockets, "q": bytes waiting unread in the receive queues, "retr": retransmitted segments}
    {"t": epoch, "k": "rtt", "ms": smoothed round trip time (kernel srtt) of the sockets that received
     data in the interval - median weighted by bytes, "to": "dl", "n": such sockets}

The round trip time is the kernel's estimate for data the app *sent* (its requests): the network
path plus the server's time to answer, refreshed whenever the app sends.  It is a property of the
connection, not of how busy the app is - the latency an app inside a limited cgroup experiences is
measured by cf.linux.net_probe, which the device starts next to the sampler.

What it does not see: UDP transports (迅雷's P2P / uTP peers - the app's disk writes, which the
runner records anyway, are the cross-check), and the bytes a connection received between the last
sample and its close.  Sockets to 127.0.0.1 / ::1 are the app talking to itself and are skipped.

    python3 -m cf.linux.netstat CONFIG [--watch S]     # what the config's download apps receive right now
"""
from __future__ import annotations

import re
import subprocess
import threading
import time
from typing import Callable, Optional

_HEAD = re.compile(r"^\s*(?:tcp\s+)?([A-Z][A-Z0-9-]*)\s+(\d+)\s+(\d+)\s+(\S+)\s+(\S+)")
_PID = re.compile(r"pid=(\d+)")
_CGROUP = re.compile(r"\bcgroup:(\S+)")
_RTT = re.compile(r"\brtt:([\d.]+)/([\d.]+)")
_RCV_RTT = re.compile(r"\brcv_rtt:([\d.]+)")
_RX = re.compile(r"\bbytes_received:(\d+)")
_ACKED = re.compile(r"\bbytes_acked:(\d+)")
_RETR = re.compile(r"\bretrans:\d+/(\d+)")
PROBE_CHILD = "cfprobe"       # == cf.linux.net_probe.CHILD
_LOOP = re.compile(r"^(127\.|\[::1\]|\[::ffff:127\.)")


def parse_ss(text: str) -> list[dict]:
    """Sockets of `ss -tinH [--cgroup] [-p] [-O]` output: [{"id", "state", "peer", "recvq", "cgroup",
    "pids", "rx", "tx", "rtt_ms", "rcv_rtt_ms", "retr"}].  One line per socket or, without -O, the
    tcp_info on an indented continuation line."""
    rows: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        if line[:1] in (" ", "\t") and rows:
            rows[-1] += " " + line.strip()
        else:
            rows.append(line)
    out = []
    for row in rows:
        m = _HEAD.match(row)
        if not m or m.group(1) in ("LISTEN", "State"):
            continue
        state, recvq, _sendq, local, peer = m.groups()
        rest = row[m.end():]

        def num(rx: re.Pattern, cast=int):
            g = rx.search(rest)
            return cast(g.group(1)) if g else None

        cg = _CGROUP.search(rest)
        out.append({"id": f"{local}>{peer}", "state": state, "peer": peer, "recvq": int(recvq),
                    "cgroup": cg.group(1) if cg else None, "pids": [int(p) for p in _PID.findall(rest)],
                    "rx": num(_RX) or 0, "tx": num(_ACKED) or 0, "rtt_ms": num(_RTT, float),
                    "rcv_rtt_ms": num(_RCV_RTT, float), "retr": num(_RETR) or 0})
    return out


def is_loopback(peer: str) -> bool:
    return bool(_LOOP.match(peer))


def weighted_median(pairs: list[tuple[float, float]]) -> Optional[float]:
    """Median of the values in [(value, weight)]; plain median when all weights are 0."""
    pairs = sorted(p for p in pairs if p[0] is not None)
    if not pairs:
        return None
    total = sum(w for _, w in pairs)
    if total <= 0:
        return pairs[len(pairs) // 2][0]
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= total / 2:
            return v
    return pairs[-1][0]


def ss_flags(by: str = "auto", run=None) -> tuple[list[str], str]:
    """(`ss` arguments, how sockets are matched to apps: "cgroup+pid" | "cgroup" | "pid").  `by`
    auto: both the socket's cgroup - when this iproute2 prints it (>= 5.7) and may resolve it - and
    the processes holding it; a socket is the app's when either says so."""
    run = run or _run_ss
    if by == "pid":
        return ["-tinH", "-p"], "pid"
    flags = ["-tinH", "--cgroup"] + (["-p"] if by == "auto" else [])
    try:
        run(flags)
        return flags, "cgroup+pid" if by == "auto" else "cgroup"
    except (OSError, subprocess.SubprocessError):
        if by == "cgroup":
            raise
    return ["-tinH", "-p"], "pid"


def _run_ss(flags: list[str]) -> str:
    r = subprocess.run(["ss", *flags], capture_output=True, text=True, timeout=10)
    if r.returncode != 0 or "cgroup2" in r.stderr:      # "Failed to open cgroup2 by ID": needs CAP_DAC_READ_SEARCH
        raise subprocess.SubprocessError(r.stderr.strip()[:200] or f"ss exit {r.returncode}")
    return r.stdout


class SockSampler(threading.Thread):
    """Samples `ss` every `every_s` seconds and keeps, per app, the records described in the module
    docstring until `read(app)` takes them.

    `groups()` -> {app: {"cgroup": "/cf/<app>" (path as ss prints it) or None, "pids": iterable}}:
    a socket is the app's when its cgroup is that path (or below it) or one of its users is one of
    the pids.  `by`: auto (both) | cgroup | pid."""

    MAX_RECORDS = 20000

    def __init__(self, groups: Callable[[], dict], every_s: float = 1.0, loopback: bool = False, by: str = "auto",
                 run=None, log=None):
        super().__init__(daemon=True)
        self.groups, self.every_s, self.loopback, self.by, self.log = groups, max(0.2, float(every_s)), loopback, by, log
        self._run = run or _run_ss
        self.flags: Optional[list[str]] = None
        self.mode = ""
        self.error = ""
        self._prev: dict[str, dict[str, dict]] = {}     # app -> socket id -> socket
        self._t_prev: Optional[float] = None
        self._out: dict[str, list[dict]] = {}
        self._lock = threading.Lock()
        self._stop_ev = threading.Event()

    # -- one sample
    def assign(self, socks: list[dict], groups: dict) -> dict[str, dict[str, dict]]:
        per: dict[str, dict[str, dict]] = {a: {} for a in groups}
        by_pid = {int(p): a for a, g in groups.items() for p in (g.get("pids") or ())}
        for s in socks:
            if not self.loopback and is_loopback(s["peer"]):
                continue
            app = None
            cg = s.get("cgroup")
            if cg and cg.endswith("/" + PROBE_CHILD):   # our own latency probe (cf.linux.net_probe), not the app
                continue
            if cg:
                app = next((a for a, g in groups.items()
                            if g.get("cgroup") and (cg == g["cgroup"] or cg.startswith(g["cgroup"].rstrip("/") + "/"))), None)
            if app is None:
                app = next((by_pid[p] for p in s["pids"] if p in by_pid), None)
            if app is not None:
                per[app][s["id"]] = s
        return per

    def tick(self, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        if self.flags is None:
            self.flags, self.mode = ss_flags(self.by, self._run)
        groups = self.groups()
        if "cgroup" not in self.mode:
            groups = {a: {**g, "cgroup": None} for a, g in groups.items()}
        per = self.assign(parse_ss(self._run(self.flags)), groups)
        dt = (now - self._t_prev) if self._t_prev is not None else None
        for app, socks in per.items():
            prev = self._prev.get(app)
            if prev is not None and dt and dt > 0:
                rx = tx = retr = 0
                active = []
                for sid, s in socks.items():
                    p = prev.get(sid)
                    fresh = p is None or s["rx"] < p["rx"]          # new connection (or the 4-tuple was reused)
                    d = s["rx"] - (0 if fresh else p["rx"])
                    rx += d
                    tx += s["tx"] - (0 if fresh else min(p["tx"], s["tx"]))
                    retr += s["retr"] - (0 if fresh else min(p["retr"], s["retr"]))
                    if d > 0 and s["rtt_ms"] is not None:
                        active.append((s["rtt_ms"], float(d)))
                recs = [{"t": round(now, 3), "k": "dl", "b": rx, "s": round(dt, 3), "tx": tx, "n": len(socks),
                         "q": sum(s["recvq"] for s in socks.values()), "retr": retr}]
                if active:
                    recs.append({"t": round(now, 3), "k": "rtt", "ms": round(weighted_median(active), 3), "to": "dl",
                                 "n": len(active)})
                with self._lock:
                    buf = self._out.setdefault(app, [])
                    buf.extend(recs)
                    if len(buf) > self.MAX_RECORDS:
                        del buf[:len(buf) - self.MAX_RECORDS]
            self._prev[app] = socks
        self._t_prev = now

    def read(self, app: str) -> list[dict]:
        with self._lock:
            return self._out.pop(app, [])

    # -- thread
    def run(self) -> None:
        due = time.monotonic()
        while not self._stop_ev.is_set():
            try:
                self.tick()
                self.error = ""
            except (OSError, subprocess.SubprocessError, ValueError) as e:
                msg = f"{type(e).__name__}: {e}"
                if msg != self.error and self.log:
                    self.log(f"socket sampler: {msg}")
                self.error = msg
                self._t_prev = None            # the next interval starts over
                self._prev.clear()
            due = max(due + self.every_s, time.monotonic() + 0.05)
            self._stop_ev.wait(max(0.0, due - time.monotonic()))

    def stop(self) -> None:
        self._stop_ev.set()


def summarize(recs: list[dict]) -> dict:
    """{"s", "mbps", "rtt_ms", "sockets", "recvq_kb"} over a list of sampler records."""
    dl = [r for r in recs if r.get("k") == "dl" and r.get("s")]
    rtt = sorted(r["ms"] for r in recs if r.get("k") == "rtt" and r.get("ms") is not None)
    sec = sum(r["s"] for r in dl)
    return {"s": round(sec, 1), "mbps": round(sum(r["b"] for r in dl) / 2 ** 20 / sec, 3) if sec > 0 else None,
            "rtt_ms": rtt[len(rtt) // 2] if rtt else None, "sockets": max((r["n"] for r in dl), default=0),
            "recvq_kb": round(max((r["q"] for r in dl), default=0) / 1024, 1)}


def main(argv=None) -> int:
    import argparse
    import json
    import os
    import shutil
    import sys

    from . import apps as A
    from .device import LinuxDesktop

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("config")
    ap.add_argument("--apps", nargs="+", help="default: the config's classes.download apps that are real apps")
    ap.add_argument("--watch", type=float, default=5.0, metavar="S", help="seconds to observe (default 5)")
    ap.add_argument("--min-kbps", type=float, default=0.0, help="exit 1 when an app receives less than this (KB/s)")
    ap.add_argument("--minutes", type=float, default=0.0, metavar="M",
                    help="also say how much these apps would write in M minutes at the speed seen, against the free "
                         "disk space; exit 3 when it does not fit")
    ap.add_argument("--reserve-gb", type=float, default=5.0, help="free space to keep (with --minutes; default 5)")
    args = ap.parse_args(argv)
    cfg = json.load(open(args.config))
    cat = A.with_overrides(cfg.get("app_overrides"), cfg.get("placeholder_apps"))
    dev = LinuxDesktop(cat=cat, notifications=False)
    ids = args.apps or [a for a in (cfg.get("classes") or {}).get("download") or []
                        if a in cat and cat[a].kind not in ("bench", "placeholder") and dev.is_installed(cat[a])]
    if not ids:
        print("no real download apps (installed) in this config")
        return 0
    probe = cfg.get("net_probe") or {}
    low = []
    seen: dict[str, float] = {}
    try:
        dev.net_start(ids, every_s=1.0, loopback=bool(probe.get("loopback")), by=probe.get("by", "auto"), strays=True)
        time.sleep(max(2.5, args.watch))
        print(f"{'app':16s} {'running':8s} {'sockets':>7s} {'MB/s':>8s} {'rtt ms':>8s}   (TCP, {dev.net.mode or '?'} match, "
              f"{args.watch:g} s)")
        for a in ids:
            r = summarize(dev.bench_read(a))
            pids = dev.pids(a) or dev.stray_pids(a)
            kbps = (r["mbps"] or 0) * 1024
            seen[a] = r["mbps"] or 0.0
            print(f"{a:16s} {'yes' if pids else 'NO':8s} {r['sockets']:7d} {r['mbps'] if r['mbps'] is not None else 0:8.3f} "
                  f"{r['rtt_ms'] if r['rtt_ms'] is not None else float('nan'):8.1f}")
            if kbps < args.min_kbps:
                low.append((a, "not running" if not pids else
                            "no TCP sockets seen for it" if not r["sockets"] else f"{kbps:.0f} KB/s"))
        if dev.net.error:
            print(f"!! {dev.net.error}", file=sys.stderr)
            return 2
    finally:
        dev.close()
    for a, why in low:
        print(f"!! {a}: {why} - start a download of a big file in it (and leave it running) before the experiment",
              file=sys.stderr)
    if low:
        return 1
    if args.minutes > 0:
        need, line = disk_budget(seen, args.minutes, {a: cat[a].cmd for a in ids})
        free = shutil.disk_usage(os.path.expanduser("~")).free / 2 ** 30
        print(f"at these speeds, in the {args.minutes:.0f} min of the run: {line}; {free:.0f} GB free in {os.path.expanduser('~')}")
        if need > free - args.reserve_gb:
            print(f"!! that is {need:.0f} GB and does not fit ({free:.0f} GB free, {args.reserve_gb:g} GB kept in reserve): lower "
                  f"the speed limit in the apps, free some space, run fewer rounds, or let the finished files be deleted during the run "
                  f"(DL_TRIM=1, cf.linux.dl_trim)", file=sys.stderr)
            return 3
    return 0


def disk_budget(mbps: dict[str, float], minutes: float, cmds: Optional[dict[str, str]] = None) -> tuple[float, str]:
    """(GB the downloads need on disk, a line saying so per app).  An app that re-uses one file
    (cf.linux.browser_dl deletes what it finished) needs that file's size, not rate x time."""
    from .browser_dl import FILE_GB
    need, parts = 0.0, []
    for a, v in mbps.items():
        if "cf.linux.browser_dl" in ((cmds or {}).get(a) or ""):
            need += FILE_GB
            parts.append(f"{a} re-uses one file ({FILE_GB:g} GB)")
        else:
            gb = v * 60.0 * minutes / 1024.0
            need += gb
            parts.append(f"{a} writes about {gb:.1f} GB")
    return need, ", ".join(parts)


if __name__ == "__main__":
    import sys
    sys.exit(main())
