"""Network latency as an application inside a cgroup experiences it - for real download apps
(百度网盘, 迅雷) that cannot be instrumented.

A tiny process that the runner places into `<the app's cgroup>/cfprobe` (a child cgroup without
controllers of its own: the app's cpu.max / cpuset / cpu.weight / memory.high apply to it exactly
as to the app's own threads, while the app's cgroup.procs - what the runner takes for "the app's
processes" - does not list it).  Twice a second it

  * notes how late it woke up (`late`): the time a thread of this cgroup waits for the CPU after
    the event it sleeps on has happened - what a packet arriving for the app waits before the app
    handles it;
  * opens a TCP connection to a fixed host (`ms`): one network round trip (SYN / SYN-ACK), address
    resolved once.

and appends {"t": epoch, "k": "rtt", "ms": float | null, "to": "ext", "late": ms} to its log - the
record format of cf.linux.bench_app, so cf.linux.classes reads both alike.

    python3 -S -E cf/linux/net_probe.py --log FILE --host www.baidu.com:443 [--every 0.5] [--timeout 2]
                                        [--cgroup /sys/fs/cgroup/cf/xunlei/cfprobe]

Only the standard library's socket / time / os are imported (run with -S -E): about 4 MB of
private memory in the app's cgroup.  It exits when the process that started it is gone.
"""
import os
import socket
import sys
import time

CHILD = "cfprobe"          # name of the child cgroup below the app's


def log_path(app: str) -> str:
    return os.path.join(os.environ.get("TMPDIR") or "/tmp", f"cf_probe_{app}.jsonl")


def parse_hostport(s: str, default_port: int = 443):
    s = s.strip()
    if s.startswith("["):                      # [v6]:port
        host, _, rest = s[1:].partition("]")
        return host, int(rest[1:]) if rest.startswith(":") else default_port
    host, sep, port = s.rpartition(":")
    if sep and port.isdigit() and ":" not in host:
        return host, int(port)
    return s, default_port


def connect_ms(addr, timeout: float):
    fam, typ, proto, sa = addr
    s = socket.socket(fam, typ, proto)
    s.settimeout(timeout)
    t0 = time.perf_counter()
    try:
        s.connect(sa)
        return round((time.perf_counter() - t0) * 1000, 2)
    finally:
        s.close()


def main(argv=None) -> int:
    args = {"--every": "0.5", "--timeout": "2", "--cgroup": "", "--count": "0"}
    argv = list(sys.argv[1:] if argv is None else argv)
    while argv:
        k = argv.pop(0)
        if k in ("-h", "--help") or not argv:
            print(__doc__)
            return 0 if k in ("-h", "--help") else 2
        args[k] = argv.pop(0)
    if "--log" not in args or "--host" not in args:
        print(__doc__)
        return 2
    if args["--cgroup"]:
        with open(os.path.join(args["--cgroup"], "cgroup.procs"), "w") as f:
            f.write(str(os.getpid()))
    host, port = parse_hostport(args["--host"])
    every, timeout, count = float(args["--every"]), float(args["--timeout"]), int(args["--count"])
    parent = os.getppid()
    fd = os.open(args["--log"], os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    addr = None
    due = time.monotonic()
    n = 0
    try:
        while os.getppid() == parent and (count <= 0 or n < count):
            late = round(max(0.0, time.monotonic() - due) * 1000, 2)
            ms = None
            try:
                if addr is None:
                    fam, typ, proto, _, sa = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0]
                    addr = (fam, typ, proto, sa)
                ms = connect_ms(addr, timeout)
            except OSError:
                addr = None                     # resolve again next time
            os.write(fd, ('{"t":%.3f,"k":"rtt","ms":%s,"to":"ext","late":%s}\n'
                          % (time.time(), "null" if ms is None else ms, late)).encode())
            n += 1
            due = max(due + every, time.monotonic() + 0.05)
            time.sleep(max(0.0, due - time.monotonic()))
    except KeyboardInterrupt:
        pass
    finally:
        os.close(fd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
