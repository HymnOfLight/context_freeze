"""Measurable compute / download apps for the three-class experiment (manual §6.9): a window that
keeps doing one fixed, repeatable job and logs how long every job took, so the very same work can
be compared in the foreground, in the background, and in the background under CPU / memory limits.

    python3 -m cf.linux.bench_app --app sevenzip --kind sevenzip                  # real 7-Zip: pack + unpack real files
    python3 -m cf.linux.bench_app --app z3 --kind z3                              # real Z3: three constraint problems
    python3 -m cf.linux.bench_app --app bench_numeric  --kind numeric             # 科学计算: LU solve + FFT + n-body
    python3 -m cf.linux.bench_app --app bench_compress --kind compress --workers 2  # 压缩 / 解压: zlib -6 of 8 MB, inflate, CRC
    python3 -m cf.linux.bench_app --app bench_download --kind download            # 下载: HTTP range requests + TCP connect RTT
    python3 -m cf.linux.bench_app --serve 18080 --rate-mbps 40                    # the local download source

Kinds
  sevenzip  the installed 7-Zip (`7z`) compresses a folder of real files and extracts it again
  z3        the installed Z3 solver decides three SMT-LIB problems with known answers
            (both: cf.linux.tool_jobs - the tool runs as a child process of the window, in the
            app's cgroup; the log has its wall time and its own CPU time)
  numeric   single-threaded pure-python kernels (LINPACK-style LU solve with partial pivoting,
            radix-2 FFT and its inverse, n-body integration); one job = one pass over the three, ~0.25 s.
  compress  `--workers` worker processes (two by default in the catalogue: the app really uses two
            cores - the case a one-core limit bites); one job = deflate an 8 MB slice of a 32 MB
            buffer, inflate it again and compare the CRC.
            The compute kinds run their jobs in child processes (fork), so the window's event loop
            keeps answering while they compute.
  download  fetches `--url` (env CF_BENCH_URL, default the local source) with HTTP Range requests,
            hashes every piece like a real downloader does and writes it to a temp file; a second
            thread measures the TCP connect round trip to the download host (and to
            `--rtt-host` / env CF_BENCH_RTT_HOST, e.g. www.baidu.com:443) twice a second.

Log (`/tmp/cf_bench_<app>.jsonl`, truncated at start, one JSON object per line; the runner copies
the new lines into every step record):
    {"t": epoch, "k": "job", "ms": wall, "cpu": CPU ms, "w": worker, "ok": true[, "op": "compress" | "extract"]}
    {"t": epoch, "k": "dl",  "b": bytes received, "s": seconds they took}
    {"t": epoch, "k": "rtt", "ms": connect time or null (timeout), "to": "dl" | "ext", "late": wake-up delay ms}
    {"t": epoch, "k": "err", "msg": "..."}
The window answers _NET_WM_PING like the placeholder does and writes the same kind of state file
(`/tmp/cf_placeholder_<app>.json`: pid + counter), so the runner's statefile oracle applies.
"""
from __future__ import annotations

import argparse
import cmath
import hashlib
import http.client
import json
import math
import os
import random
import signal
import socket
import sys
import tempfile
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import urlsplit

DEFAULT_PORT = 18080
DEFAULT_URL = f"http://127.0.0.1:{DEFAULT_PORT}/stream"
KINDS = ("numeric", "compress", "download", "sevenzip", "z3")


def log_path(app: str) -> str:
    return os.path.join(tempfile.gettempdir(), f"cf_bench_{app}.jsonl")


def state_path(app: str) -> str:
    return os.path.join(tempfile.gettempdir(), f"cf_placeholder_{app}.json")


def read_new(path: str, offset: int) -> tuple[list[dict], int]:
    """Records appended to a bench log since `offset` (bytes) and the new offset.  A log that shrank
    (the app was restarted) is read from the start; a half-written last line is left for next time."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return [], offset
    if size < offset:
        offset = 0
    if size == offset:
        return [], offset
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read(size - offset)
    end = data.rfind(b"\n")
    if end < 0:
        return [], offset
    out = []
    for line in data[:end].splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict) and "k" in r and "t" in r:
            out.append(r)
    return out, offset + end + 1


class BenchLog:
    """Line-buffered JSONL, always opened O_APPEND so the app and its worker processes can write to
    the same file; the app's own instance (append=False) empties it first."""

    def __init__(self, path: str, append: bool = False):
        self.path = path
        if not append:
            open(path, "w").close()
        self._f = open(path, "a", buffering=1, encoding="utf-8")
        self._lock = threading.Lock()

    def put(self, k: str, **kw) -> None:
        line = json.dumps({"t": round(time.time(), 3), "k": k, **kw}, separators=(",", ":"))
        with self._lock:
            try:
                self._f.write(line + "\n")
            except (OSError, ValueError):
                pass

    def close(self) -> None:
        with self._lock:
            try:
                self._f.close()
            except OSError:
                pass


# ------------------------------------------------------------------ numeric kernels (pure python)
def k_lu(n: int = 160, seed: int = 1) -> float:
    """Solve A x = b (b = row sums, so x = 1) by LU with partial pivoting; returns max |x - 1|."""
    r = random.Random(seed)
    a = [[r.random() for _ in range(n)] for _ in range(n)]
    b = [sum(row) for row in a]
    for k in range(n):
        p = max(range(k, n), key=lambda i: abs(a[i][k]))
        if p != k:
            a[k], a[p] = a[p], a[k]
            b[k], b[p] = b[p], b[k]
        akk, rk = a[k][k], a[k]
        for i in range(k + 1, n):
            f = a[i][k] / akk
            if f:
                ri = a[i]
                for j in range(k + 1, n):
                    ri[j] -= f * rk[j]
                b[i] -= f * b[k]
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        x[i] = (b[i] - sum(a[i][j] * x[j] for j in range(i + 1, n))) / a[i][i]
    return max(abs(v - 1.0) for v in x)


def k_fft(k: int = 15) -> float:
    """Radix-2 FFT of 2^k samples of two sines, then the inverse; returns the round-trip error."""
    n = 1 << k
    src = [complex(math.sin(0.01 * i) + 0.5 * math.sin(0.37 * i), 0.0) for i in range(n)]

    def fft(x: list, sign: float) -> list:
        x = list(x)
        j = 0
        for i in range(1, n):
            bit = n >> 1
            while j & bit:
                j ^= bit
                bit >>= 1
            j |= bit
            if i < j:
                x[i], x[j] = x[j], x[i]
        m = 2
        while m <= n:
            w_m = cmath.exp(sign * 2j * math.pi / m)
            half = m // 2
            for s in range(0, n, m):
                w = 1 + 0j
                for t in range(half):
                    u, v = x[s + t], x[s + t + half] * w
                    x[s + t], x[s + t + half] = u + v, u - v
                    w *= w_m
            m <<= 1
        return x

    back = fft(fft(src, -1.0), 1.0)
    return max(abs(back[i] / n - src[i]) for i in range(0, n, 97))


def k_nbody(steps: int = 15000) -> float:
    """Five bodies, symplectic Euler; returns the relative energy drift."""
    bodies = [[0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 39.47], [4.84, -1.16, -0.10, 0.606, 2.81, -0.02, 0.037],
              [8.34, 4.12, -0.4, -1.01, 1.82, 0.008, 0.011], [12.89, -15.11, -0.22, 1.08, 0.868, -0.01, 0.0017],
              [15.37, -25.9, 0.179, 0.979, 0.594, -0.034, 0.002]]

    def energy() -> float:
        e = 0.0
        for i, b in enumerate(bodies):
            e += 0.5 * b[6] * (b[3] * b[3] + b[4] * b[4] + b[5] * b[5])
            for c in bodies[i + 1:]:
                e -= b[6] * c[6] / math.sqrt((b[0] - c[0]) ** 2 + (b[1] - c[1]) ** 2 + (b[2] - c[2]) ** 2)
        return e

    e0, dt = energy(), 0.001
    for _ in range(steps):
        for i in range(5):
            bi = bodies[i]
            for j in range(i + 1, 5):
                bj = bodies[j]
                dx, dy, dz = bi[0] - bj[0], bi[1] - bj[1], bi[2] - bj[2]
                d2 = dx * dx + dy * dy + dz * dz
                mag = dt / (d2 * math.sqrt(d2))
                bi[3] -= dx * bj[6] * mag; bi[4] -= dy * bj[6] * mag; bi[5] -= dz * bj[6] * mag
                bj[3] += dx * bi[6] * mag; bj[4] += dy * bi[6] * mag; bj[5] += dz * bi[6] * mag
        for b in bodies:
            b[0] += dt * b[3]; b[1] += dt * b[4]; b[2] += dt * b[5]
    return abs((energy() - e0) / e0)


def numeric_job(scale: float = 1.0) -> bool:
    """One pass over the three kernels; True when every result is within its tolerance."""
    lu = k_lu(max(8, int(160 * scale ** (1 / 3))))
    ff = k_fft(15 if scale >= 1 else 10)
    nb = k_nbody(max(50, int(15000 * scale)))
    return lu < 1e-6 and ff < 1e-6 and nb < 1e-2


# ------------------------------------------------------------------ compress / inflate
def make_corpus(mb: int = 32, seed: int = 7) -> bytes:
    """Text-like data (words from a small vocabulary + a little noise): deflates about 2 : 1."""
    r = random.Random(seed)
    words = [bytes(r.randrange(97, 123) for _ in range(r.randrange(3, 10))) for _ in range(4000)]
    buf = bytearray()
    while len(buf) < mb * 2 ** 20:
        buf += b" ".join(r.choices(words, k=20000)) + r.randbytes(2048)
    return bytes(buf[:mb * 2 ** 20])


def compress_job(corpus: bytes, n: int, slice_mb: int = 8, level: int = 6) -> bool:
    """Deflate the n-th slice of the corpus, inflate it, compare CRCs."""
    size = slice_mb * 2 ** 20
    slots = max(1, len(corpus) // size)
    off = (n % slots) * size
    src = corpus[off:off + size]
    packed = zlib.compress(src, level)
    return zlib.crc32(zlib.decompress(packed)) == zlib.crc32(src) and len(packed) < len(src)


class JobWorker(threading.Thread):
    """Runs `job(n)` back to back and logs wall / CPU time of every one."""

    def __init__(self, idx: int, job, log: BenchLog, stop, on_job=None):
        super().__init__(daemon=True)
        self.idx, self.job, self.log, self.stop_ev = idx, job, log, stop
        self.on_job = on_job
        self.done = 0
        self.last_ms = 0.0
        self.bad = 0

    def run(self) -> None:
        n = self.idx                     # workers start on different slices
        while not self.stop_ev.is_set():
            t0, c0 = time.perf_counter(), time.thread_time()
            try:
                ok = bool(self.job(n))
            except Exception as e:      # noqa: BLE001 - a MemoryError under memory.max would land here
                ok = False
                self.log.put("err", msg=f"{type(e).__name__}: {e}"[:120])
                self.stop_ev.wait(0.5)
            ms = (time.perf_counter() - t0) * 1000
            cpu = (time.thread_time() - c0) * 1000
            self.log.put("job", ms=round(ms, 2), cpu=round(cpu, 2), w=self.idx, ok=ok)
            self.done += 1
            self.bad += 0 if ok else 1
            self.last_ms = ms
            if self.on_job:
                self.on_job(self)
            n += 1


def _die_with_parent() -> None:
    """Linux: have the kernel kill this process when its parent goes away (the app was killed with
    SIGKILL, e.g. by the OOM killer or `cgroup.kill`) - a worker must never outlive its window."""
    try:
        import ctypes
        import signal
        ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL)      # PR_SET_PDEATHSIG
    except Exception:   # noqa: BLE001
        pass


class ProcWorker:
    """A JobWorker in a child process of its own (fork).  The compute kinds are CPU-bound python: as
    a thread they would hold the GIL and the window's event loop (the _NET_WM_PING answers) would
    stall behind every job; as a process the app also really is a multi-process application whose
    cgroup limits apply to all of them."""

    def __init__(self, idx: int, job, log_path_: str, stop):
        import multiprocessing
        self._ctx = multiprocessing.get_context("fork")
        self.idx, self.job, self.log_path, self.stop_ev = idx, job, log_path_, stop
        self._done = self._ctx.Value("q", 0, lock=False)
        self._bad = self._ctx.Value("q", 0, lock=False)
        self._last = self._ctx.Value("d", 0.0, lock=False)
        self.proc = None

    done = property(lambda self: self._done.value)
    bad = property(lambda self: self._bad.value)
    last_ms = property(lambda self: self._last.value)

    def _main(self, ppid: int) -> None:
        _die_with_parent()
        if os.getppid() != ppid:          # the parent died before prctl took effect
            os._exit(0)

        def on_job(w: JobWorker) -> None:
            self._done.value, self._bad.value, self._last.value = w.done, w.bad, w.last_ms
            if os.getppid() != ppid:
                self.stop_ev.set()

        log = BenchLog(self.log_path, append=True)
        try:
            JobWorker(self.idx, self.job, log, self.stop_ev, on_job=on_job).run()
        except KeyboardInterrupt:
            pass
        finally:
            log.close()

    def start(self) -> None:
        self.proc = self._ctx.Process(target=self._main, args=(os.getpid(),), daemon=True)
        self.proc.start()

    def join(self, timeout: Optional[float] = None) -> None:
        if self.proc is not None:
            self.proc.join(timeout)
            if self.proc.is_alive():
                self.proc.terminate()


# ------------------------------------------------------------------ download + RTT
def parse_hostport(s: str, default_port: int = 443) -> tuple[str, int]:
    s = s.strip()
    if "://" in s:
        u = urlsplit(s)
        return u.hostname or "", u.port or (443 if u.scheme == "https" else 80)
    host, _, port = s.rpartition(":")
    if host and port.isdigit():
        return host, int(port)
    return s, default_port


class Downloader(threading.Thread):
    """HTTP Range download in `chunk_mb` pieces, each hashed (as a real client verifies pieces) and
    written to `out_path` (overwritten from the start every 256 MB); logs bytes per ~second."""

    def __init__(self, url: str, log: BenchLog, stop: threading.Event, chunk_mb: int = 8,
                 out_path: Optional[str] = None, report_s: float = 1.0):
        super().__init__(daemon=True)
        self.url, self.log, self.stop_ev = url, log, stop
        self.chunk = chunk_mb * 2 ** 20
        self.out_path = out_path or os.path.join(tempfile.gettempdir(), f"cf_bench_dl_{os.getpid()}.bin")
        self.report_s = report_s
        self.total = 0
        self.pieces = 0
        self.errors = 0
        self.last_mbps = 0.0

    def _connect(self):
        u = urlsplit(self.url)
        cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
        return cls(u.hostname, u.port or (443 if u.scheme == "https" else 80), timeout=10), (u.path or "/") + (
            f"?{u.query}" if u.query else "")

    def run(self) -> None:
        conn, off, size = None, 0, None
        last, acc = time.time(), 0
        try:
            out = open(self.out_path, "wb")
        except OSError as e:
            self.log.put("err", msg=f"open {self.out_path}: {e}"[:120])
            return
        try:
            while not self.stop_ev.is_set():
                try:
                    if conn is None:
                        conn, path = self._connect()
                    conn.request("GET", path, headers={"Range": f"bytes={off}-{off + self.chunk - 1}",
                                                       "User-Agent": "cf-bench/1"})
                    r = conn.getresponse()
                    if r.status not in (200, 206):
                        r.read()
                        raise OSError(f"HTTP {r.status}")
                    cr = r.getheader("Content-Range") or ""
                    if "/" in cr and cr.rsplit("/", 1)[1].isdigit():
                        size = int(cr.rsplit("/", 1)[1])
                    h = hashlib.sha256()
                    while not self.stop_ev.is_set():
                        b = r.read(65536)
                        if not b:
                            break
                        h.update(b)
                        out.write(b)
                        acc += len(b)
                        self.total += len(b)
                        now = time.time()
                        if now - last >= self.report_s:
                            self.last_mbps = acc / 2 ** 20 / (now - last)
                            self.log.put("dl", b=acc, s=round(now - last, 3))
                            last, acc = now, 0
                    self.pieces += 1
                    if out.tell() >= 256 * 2 ** 20:
                        out.seek(0)
                    off = 0 if r.status == 200 else off + self.chunk
                    if size and off >= size:
                        off = 0
                except (OSError, http.client.HTTPException) as e:
                    self.errors += 1
                    self.log.put("err", msg=f"download: {type(e).__name__}: {e}"[:120])
                    try:
                        if conn is not None:
                            conn.close()
                    except OSError:
                        pass
                    conn = None
                    self.stop_ev.wait(1.0)
        finally:
            out.close()
            try:
                os.remove(self.out_path)
            except OSError:
                pass


class RttProbe(threading.Thread):
    """TCP connect round trip to each target every `every_s` seconds (address resolved once), plus how
    late the probe itself woke up."""

    def __init__(self, targets: list[tuple[str, str, int]], log: BenchLog, stop: threading.Event,
                 every_s: float = 0.5, timeout_s: float = 3.0):
        super().__init__(daemon=True)
        self.targets, self.log, self.stop_ev = targets, log, stop
        self.every_s, self.timeout_s = every_s, timeout_s
        self.last: dict[str, Optional[float]] = {}
        self.last_late = 0.0
        self._addr: dict[str, tuple] = {}

    def _resolve(self, label: str, host: str, port: int):
        if label not in self._addr:
            fam, typ, proto, _, sa = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0]
            self._addr[label] = (fam, typ, proto, sa)
        return self._addr[label]

    def once(self, label: str, host: str, port: int) -> Optional[float]:
        try:
            fam, typ, proto, sa = self._resolve(label, host, port)
            s = socket.socket(fam, typ, proto)
            s.settimeout(self.timeout_s)
            t0 = time.perf_counter()
            try:
                s.connect(sa)
                return round((time.perf_counter() - t0) * 1000, 2)
            finally:
                s.close()
        except OSError:
            self._addr.pop(label, None)
            return None

    def run(self) -> None:
        due = time.monotonic()
        while not self.stop_ev.is_set():
            # `late`: how long after its due time this thread got to run - what a packet arriving for
            # a CPU-limited app waits before the app handles it (0 when the app is not held back)
            late = round(max(0.0, time.monotonic() - due) * 1000, 2)
            self.last_late = late
            for label, host, port in self.targets:
                ms = self.once(label, host, port)
                self.last[label] = ms
                self.log.put("rtt", ms=ms, to=label, late=late)
            due = max(due + self.every_s, time.monotonic() + 0.05)
            self.stop_ev.wait(max(0.0, due - time.monotonic()))


# ------------------------------------------------------------------ local download source
VIRTUAL_SIZE = 1 << 40


def make_server(port: int = DEFAULT_PORT, rate_mbps: float = 40.0, bind: str = "127.0.0.1") -> ThreadingHTTPServer:
    """An HTTP server whose every path is one endless (1 TiB) pseudo-random file with Range support,
    sent at most at `rate_mbps` MB/s per connection (0 = as fast as the loopback goes)."""
    block = random.Random(11).randbytes(1 << 20)
    rate = rate_mbps * 2 ** 20

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a) -> None:   # noqa: D401 - quiet
            pass

        def _range(self) -> tuple[int, int, bool]:
            rng = self.headers.get("Range") or ""
            if rng.startswith("bytes=") and "-" in rng:
                a, _, b = rng[6:].partition("-")
                if a.isdigit():
                    start = int(a)
                    end = int(b) if b.isdigit() else VIRTUAL_SIZE - 1
                    return start, min(end, VIRTUAL_SIZE - 1), True
            return 0, VIRTUAL_SIZE - 1, False

        def _head(self) -> tuple[int, int]:
            start, end, partial = self._range()
            self.send_response(206 if partial else 200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            if partial:
                self.send_header("Content-Range", f"bytes {start}-{end}/{VIRTUAL_SIZE}")
            self.end_headers()
            return start, end

        def do_HEAD(self) -> None:   # noqa: N802
            self._head()

        def do_GET(self) -> None:    # noqa: N802
            start, end = self._head()
            left, pos = end - start + 1, start % len(block)
            t0, sent = time.monotonic(), 0
            try:
                while left > 0:
                    n = min(left, 65536, len(block) - pos)
                    self.wfile.write(block[pos:pos + n])
                    pos = (pos + n) % len(block)
                    left -= n
                    sent += n
                    if rate > 0:
                        ahead = sent / rate - (time.monotonic() - t0)
                        if ahead > 0.002:
                            time.sleep(ahead)
            except (BrokenPipeError, ConnectionResetError, socket.timeout):
                self.close_connection = True

    srv = ThreadingHTTPServer((bind, port), Handler)
    srv.daemon_threads = True
    return srv


def url_reachable(url: str, timeout: float = 5.0) -> Optional[str]:
    """None when a small Range request to `url` succeeds, else the reason."""
    u = urlsplit(url)
    cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    try:
        c = cls(u.hostname, u.port or (443 if u.scheme == "https" else 80), timeout=timeout)
        c.request("GET", (u.path or "/") + (f"?{u.query}" if u.query else ""), headers={"Range": "bytes=0-1023"})
        r = c.getresponse()
        r.read(1024)
        c.close()
        return None if r.status in (200, 206) else f"HTTP {r.status}"
    except (OSError, http.client.HTTPException) as e:
        return f"{type(e).__name__}: {e}"


# ------------------------------------------------------------------ the app
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--app", default=None, help="catalogue id; WM_CLASS becomes cf-<app> (default bench_<kind>)")
    ap.add_argument("--kind", choices=KINDS, default="numeric")
    ap.add_argument("--title", default=None)
    ap.add_argument("--workers", type=int, default=1, help="compress: worker processes (each really uses a core)")
    ap.add_argument("--scale", type=float, default=1.0, help="numeric: job size factor (1 = ~0.25 s)")
    ap.add_argument("--slice-mb", type=int, default=8, help="compress: MB deflated per job")
    ap.add_argument("--corpus-mb", type=float, default=None,
                    help="compress: MB of input kept in memory (32); sevenzip: MB of real files packed per job (4)")
    ap.add_argument("--level", type=int, default=5, help="sevenzip: -mx (5 = 7-Zip's default)")
    ap.add_argument("--threads", type=int, default=2, help="sevenzip: -mmt")
    ap.add_argument("--tool", default=None, help="sevenzip / z3: path of the binary (default: found on PATH)")
    ap.add_argument("--work-dir", default=None, help="sevenzip / z3: scratch folder (default /tmp/cf_bench_<app>.d)")
    ap.add_argument("--url", default=None, help=f"download: source (env CF_BENCH_URL, default {DEFAULT_URL})")
    ap.add_argument("--chunk-mb", type=int, default=8, help="download: MB per Range request")
    ap.add_argument("--rtt-host", default=None, help="download: extra host:port for the connect RTT (env CF_BENCH_RTT_HOST)")
    ap.add_argument("--rtt-every", type=float, default=0.5)
    ap.add_argument("--log", default=None, help="job log (default /tmp/cf_bench_<app>.jsonl)")
    ap.add_argument("--state-file", default=None)
    ap.add_argument("--geometry", default="560x200")
    ap.add_argument("--serve", type=int, metavar="PORT", help="run the local download source instead of an app")
    ap.add_argument("--rate-mbps", type=float, default=40.0, help="--serve: MB/s per connection (0 = unlimited)")
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--check-url", metavar="URL", help="exit 0 when a Range request to URL works")
    args = ap.parse_args(argv)

    if args.check_url:
        why = url_reachable(args.check_url)
        print("ok" if why is None else why)
        return 0 if why is None else 1
    if args.serve:
        srv = make_server(args.serve, args.rate_mbps, args.bind)
        print(f"cf bench download source on http://{args.bind}:{args.serve}/stream  "
              f"({args.rate_mbps:g} MB/s per connection)" if args.rate_mbps > 0 else
              f"cf bench download source on http://{args.bind}:{args.serve}/stream (unlimited)", flush=True)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        return 0

    app = args.app or f"bench_{args.kind}"
    title = args.title or app
    try:
        import Xlib  # noqa: F401
    except ImportError:
        print("python3-xlib is required: sudo apt install python3-xlib", file=sys.stderr)
        return 2
    from .placeholder_app import window_loop

    tool = None
    if args.kind in ("sevenzip", "z3"):
        from . import tool_jobs
        tool = args.tool or tool_jobs.find_tool(args.kind)
        if not tool:
            print(f"{'/'.join(tool_jobs.TOOLS[args.kind])} not found: {tool_jobs.INSTALL[args.kind]}", file=sys.stderr)
            return 3
        work = args.work_dir or os.path.join(tempfile.gettempdir(), f"cf_bench_{app}.d")
        os.makedirs(work, exist_ok=True)
    log = BenchLog(args.log or log_path(app))
    started = time.time()
    workers: list = []
    stop = threading.Event()
    if args.kind in ("numeric", "compress"):
        import multiprocessing
        stop = multiprocessing.get_context("fork").Event()
    dl: Optional[Downloader] = None
    probe: Optional[RttProbe] = None
    if args.kind == "numeric":
        workers = [ProcWorker(0, lambda n: numeric_job(args.scale), log.path, stop)]
        what = f"numeric: LU + FFT + n-body, scale {args.scale:g}, 1 worker process"
    elif args.kind == "compress":
        corpus = make_corpus(int(args.corpus_mb or 32))
        workers = [ProcWorker(i, lambda n: compress_job(corpus, n, args.slice_mb), log.path, stop)
                   for i in range(max(1, args.workers))]
        what = f"compress: zlib -6 of {args.slice_mb} MB + inflate + crc, {len(workers)} worker process(es)"
    elif args.kind == "sevenzip":
        size, n = tool_jobs.build_corpus(os.path.join(work, "corpus"), args.corpus_mb or 4)
        workers = [tool_jobs.ToolWorker(tool_jobs.sevenzip_steps(tool, work, args.level, args.threads), log, stop)]
        what = f"{tool} a -mx={args.level} -mmt={args.threads} + x: {n} files, {size / 2 ** 20:.1f} MB"
    elif args.kind == "z3":
        steps = tool_jobs.z3_steps(tool, work)
        workers = [tool_jobs.ToolWorker(steps, log, stop, per_op=False)]
        what = f"{tool}: " + ", ".join(f"{s.op} ({s.expect})" for s in steps)
    else:
        url = args.url or os.environ.get("CF_BENCH_URL") or DEFAULT_URL
        u = urlsplit(url)
        targets = [("dl", u.hostname or "127.0.0.1", u.port or (443 if u.scheme == "https" else 80))]
        ext = args.rtt_host or os.environ.get("CF_BENCH_RTT_HOST") or ""
        if ext:
            targets.append(("ext", *parse_hostport(ext)))
        dl = Downloader(url, log, stop, chunk_mb=args.chunk_mb)
        probe = RttProbe(targets, log, stop, every_s=args.rtt_every)
        what = f"download: {url[:60]}  rtt -> {', '.join(f'{h}:{p}' for _, h, p in targets)}"
    log.put("start", kind=args.kind, pid=os.getpid(), what=what)
    for th in (*workers, dl, probe):
        if th is not None:
            th.start()

    def counter() -> int:
        return sum(w.done for w in workers) if workers else (dl.total >> 20 if dl else 0)

    def state() -> dict:
        return {"counter": counter(), "elapsed_s": round(time.time() - started, 1), "kind": args.kind,
                "pid": os.getpid(), "ts": time.time()}

    def lines(pings: int) -> list[str]:
        out = [f"{title}   (cf bench, pid {os.getpid()})", what[:86]]
        if workers:
            last = ", ".join(", ".join(f"{op} {ms:.0f} ms" for op, ms in w.last.items()) if getattr(w, "last", None)
                             else f"{w.last_ms:.0f} ms" for w in workers)
            out.append(f"jobs done {counter()}   last {last}"
                       + (f"   FAILED {sum(w.bad for w in workers)}" if any(w.bad for w in workers) else ""))
        if dl is not None and probe is not None:
            out.append(f"received {dl.total >> 20} MB   now {dl.last_mbps:.1f} MB/s   errors {dl.errors}")
            out.append("rtt " + "   ".join(f"{k} {'timeout' if v is None else f'{v:.1f} ms'}" for k, v in probe.last.items())
                       + f"   woke {probe.last_late:.1f} ms late")
        out.append(f"elapsed {time.time() - started:.0f} s    pings answered {pings}")
        out.append(f"log {log.path}")
        return out

    def on_exit() -> None:
        stop.set()
        for w in workers:
            if hasattr(w, "kill"):
                w.kill()               # the tool's current run: nobody waits for its result any more
            w.join(3.0)
        log.close()

    def on_term(*_a) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, on_term)     # leave through window_loop's cleanup like on WM_DELETE

    return window_loop(app, title, args.geometry, lines, state, args.state_file or state_path(app), on_exit=on_exit)


if __name__ == "__main__":
    sys.exit(main())
