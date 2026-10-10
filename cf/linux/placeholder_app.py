"""Stand-in desktop application with a verifiable workload (for apps that have no Linux build).

    python3 -m cf.linux.placeholder_app --app xiaoyi_sim --title 小艺(占位) --cpu 0.6 --mem 400
    python3 -m cf.linux.placeholder_app --app sim_download --io-mbps 20       # writes a temp file
    python3 -m cf.linux.placeholder_app --app sim_push --notify-every 20      # notify-send every 20 s

A plain X11 window (python3-xlib, no toolkit) with WM_CLASS "cf-<app>", answering _NET_WM_PING
like GTK / Qt / Electron apps do, so the runner can measure its responsiveness.  A worker thread
keeps the CPU busy for `--cpu` of the time, touches a `--mem` MB buffer so it stays resident
(or gets reclaimed when memory.high says so), optionally streams data to disk and optionally
emits desktop notifications.  The progress counter is shown in the window and written once a
second to the state file (`/tmp/cf_placeholder_<app>.json`), which lets you check two things
after a throttled background phase: the counter is still increasing (progress continues, slower)
and never reset (state and context survived).
"""
from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import select
import subprocess
import sys
import tempfile
import threading
import time


def _burn(cpu: float, counter) -> None:
    """Extra CPU worker process (the GIL caps a single python process at ~1 core).

    Children inherit the parent's cgroup, so the whole bundle is throttled together.
    `counter` is a shared Value so the window can show total progress."""
    slice_s = 0.1
    while True:
        t0 = time.monotonic()
        busy_until = t0 + slice_s * cpu
        x = 0.0
        while time.monotonic() < busy_until:
            for i in range(2000):
                x += (i * 0.5) ** 0.5
        with counter.get_lock():
            counter.value += 1
        rest = slice_s - (time.monotonic() - t0)
        if rest > 0:
            time.sleep(rest)


class Worker(threading.Thread):
    def __init__(self, cpu: float, mem_mb: int, io_mbps: float, notify_every: float, title: str,
                 workers: int = 1):
        super().__init__(daemon=True)
        self.cpu = max(0.0, min(1.0, cpu))
        self.mem_mb = mem_mb
        self.io_mbps = io_mbps
        self.notify_every = notify_every
        self.title = title
        self.counter = 0
        self.extra_counter = multiprocessing.Value("q", 0)
        self.procs = [multiprocessing.Process(target=_burn, args=(self.cpu, self.extra_counter), daemon=True)
                      for _ in range(max(0, workers - 1))]
        for p in self.procs:
            p.start()
        self.bytes_written = 0
        self.notifications = 0
        self.started = time.time()
        self.stop = threading.Event()
        self.buf = bytearray(mem_mb * 2 ** 20) if mem_mb > 0 else bytearray()
        self._io_fh = None
        if io_mbps > 0:
            self._io_path = os.path.join(tempfile.gettempdir(), f"cf_placeholder_io_{os.getpid()}.bin")
            self._io_fh = open(self._io_path, "wb")
        self._touch_pos = 0
        self._last_notify = time.time()

    def run(self) -> None:
        slice_s = 0.1
        chunk = bytes(1 << 20)
        while not self.stop.is_set():
            t0 = time.monotonic()
            busy_until = t0 + slice_s * self.cpu
            x = 0.0
            while time.monotonic() < busy_until:
                for i in range(2000):
                    x += (i * 0.5) ** 0.5
            self.counter += 1
            if self.buf:
                # touch 1/50 of the buffer per slice so that the whole buffer is referenced every ~5 s
                step = max(1, len(self.buf) // 50)
                end = min(len(self.buf), self._touch_pos + step)
                for off in range(self._touch_pos, end, 4096):
                    self.buf[off] = (self.buf[off] + 1) & 0xFF
                self._touch_pos = 0 if end >= len(self.buf) else end
            if self._io_fh:
                n = int(self.io_mbps * slice_s)
                for _ in range(max(0, n)):
                    self._io_fh.write(chunk)
                    self.bytes_written += len(chunk)
                self._io_fh.flush()
                if self.bytes_written > 512 * 2 ** 20:         # keep the temp file small
                    self._io_fh.seek(0)
            if self.notify_every > 0 and time.time() - self._last_notify >= self.notify_every:
                self._last_notify = time.time()
                self.notifications += 1
                try:
                    subprocess.Popen(["notify-send", "-a", self.title, f"{self.title} 消息 #{self.notifications}",
                                      f"counter={self.counter}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except OSError:
                    pass
            rest = slice_s - (time.monotonic() - t0)
            if rest > 0:
                self.stop.wait(rest)
        if self._io_fh:
            self._io_fh.close()
            try:
                os.remove(self._io_path)
            except OSError:
                pass
        for p in self.procs:
            p.terminate()

    def state(self) -> dict:
        return {"counter": self.counter, "elapsed_s": round(time.time() - self.started, 1),
                "bytes_written": self.bytes_written, "notifications": self.notifications,
                "worker_counter": self.extra_counter.value, "workers": len(self.procs) + 1,
                "pid": os.getpid(), "ts": time.time()}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--app", default="sim_compute", help="catalogue id; WM_CLASS becomes cf-<app>")
    ap.add_argument("--title", default=None)
    ap.add_argument("--cpu", type=float, default=0.5, help="busy fraction of one core per worker (0..1)")
    ap.add_argument("--workers", type=int, default=1,
                    help="CPU workers (1 thread + N-1 child processes); >1 lets the app exceed one core "
                         "so a 1-core cpu.max quota really binds")
    ap.add_argument("--mem", type=int, default=300, help="MB of anonymous memory kept resident")
    ap.add_argument("--io-mbps", type=float, default=0.0, help="MB/s written to a temp file")
    ap.add_argument("--notify-every", type=float, default=0.0, help="seconds between notify-send calls")
    ap.add_argument("--state-file", default=None)
    ap.add_argument("--geometry", default="520x240")
    args = ap.parse_args(argv)
    title = args.title or args.app
    state_file = args.state_file or os.path.join(tempfile.gettempdir(), f"cf_placeholder_{args.app}.json")

    try:
        from Xlib import X, Xutil, display
        from Xlib.protocol import event as xevent
    except ImportError:
        print("python3-xlib is required: sudo apt install python3-xlib", file=sys.stderr)
        return 2

    d = display.Display()
    scr = d.screen()
    w, h = (int(x) for x in args.geometry.split("x"))
    win = scr.root.create_window(60, 60, w, h, 1, scr.root_depth, X.InputOutput, X.CopyFromParent,
                                 background_pixel=scr.white_pixel,
                                 event_mask=X.ExposureMask | X.StructureNotifyMask | X.KeyPressMask)
    gc = win.create_gc(foreground=scr.black_pixel, background=scr.white_pixel)
    WM_DELETE = d.intern_atom("WM_DELETE_WINDOW")
    NET_WM_PING = d.intern_atom("_NET_WM_PING")
    WM_PROTOCOLS = d.intern_atom("WM_PROTOCOLS")
    NET_WM_PID = d.intern_atom("_NET_WM_PID")
    # WM_NAME is a latin-1 STRING; the UTF-8 title goes to _NET_WM_NAME (what modern WMs show)
    win.change_property(d.intern_atom("_NET_WM_NAME"), d.intern_atom("UTF8_STRING"), 8, title.encode("utf-8"))
    win.set_wm_name(title.encode("latin-1", "replace").decode("latin-1"))
    win.set_wm_class(f"cf-{args.app}", f"cf-{args.app}")
    win.set_wm_protocols([WM_DELETE, NET_WM_PING])
    win.change_property(NET_WM_PID, d.intern_atom("CARDINAL"), 32, [os.getpid()])
    win.set_wm_hints(flags=Xutil.StateHint, initial_state=Xutil.NormalState)
    win.map()
    d.flush()

    worker = Worker(args.cpu, args.mem, args.io_mbps, args.notify_every, title, workers=args.workers)
    worker.start()
    pings = 0

    def draw() -> None:
        win.clear_area(0, 0, 0, 0, 0)
        st = worker.state()
        lines = [f"{title}   (cf placeholder, pid {os.getpid()})",
                 f"counter   {st['counter']}  (+{st['worker_counter']} in {st['workers'] - 1} child workers)"
                 f"      elapsed {st['elapsed_s']} s",
                 f"workload  cpu {args.cpu:.0%} x {args.workers} worker(s), mem {args.mem} MB"
                 + (f", io {args.io_mbps:g} MB/s" if args.io_mbps else "")
                 + (f", notify every {args.notify_every:g} s" if args.notify_every else ""),
                 f"written   {st['bytes_written'] // 2 ** 20} MB    notifications {st['notifications']}"
                 f"    pings answered {pings}",
                 f"state     {state_file}"]
        for i, text in enumerate(lines):
            win.draw_text(gc, 12, 24 + i * 20, text.encode("latin-1", "replace"))
        d.flush()

    def write_state() -> None:
        tmp = state_file + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(worker.state(), f)
            os.replace(tmp, state_file)
        except OSError:
            pass

    last = 0.0
    try:
        while True:
            while d.pending_events():
                e = d.next_event()
                if e.type == X.Expose:
                    draw()
                elif e.type == X.ClientMessage and e.client_type == WM_PROTOCOLS:
                    fmt, data = e.data
                    if data[0] == WM_DELETE:
                        raise KeyboardInterrupt
                    if data[0] == NET_WM_PING:
                        # reply: same message to the root window (EWMH)
                        reply = xevent.ClientMessage(window=scr.root, client_type=WM_PROTOCOLS, data=(32, list(data)))
                        scr.root.send_event(reply, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
                        d.flush()
                        pings += 1
                elif e.type == X.DestroyNotify:
                    raise KeyboardInterrupt
            now = time.time()
            if now - last >= 1.0:
                last = now
                write_state()
                draw()
            r, _, _ = select.select([d.fileno()], [], [], 0.25)
    except KeyboardInterrupt:
        pass
    finally:
        worker.stop.set()
        write_state()
        try:
            d.close()
        except Exception:   # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
