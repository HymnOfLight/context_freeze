"""Three-class experiment with real applications (manual §6.9): the compute apps drive the installed
7-Zip / Z3 (cf.linux.tool_jobs), the download apps are measured through the kernel's TCP statistics
of their sockets (cf.linux.netstat)."""
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from fake_linux_desktop import OVERRIDES, FakeLinuxDesktop, FakeWorkDriver  # noqa: E402

from cf.analyze import load  # noqa: E402
from cf.linux import apps as A  # noqa: E402
from cf.linux import bench_app as B  # noqa: E402
from cf.linux import classes as C  # noqa: E402
from cf.linux import netstat as N  # noqa: E402
from cf.linux import tool_jobs as J  # noqa: E402
from cf.linux.runner import ThrottleExperiment  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ------------------------------------------------------------------ ss parser
SS_CGROUP = """\
ESTAB 0       0           192.168.64.5:51234       111.206.1.9:443   cgroup:/cf/baidunetdisk
\t cubic wscale:7,7 rto:232 rtt:31.5/4.25 ato:40 mss:1448 pmtu:1500 rcvmss:1448 advmss:1448 cwnd:10 bytes_sent:1200 bytes_acked:1201 bytes_received:5000000 segs_out:900 segs_in:3600 send 3.7Mbps lastsnd:40 pacing_rate 7.4Mbps delivered:12 app_limited rcv_rtt:30.2 rcv_space:262144 rcv_ssthresh:1048576 minrtt:28.1
ESTAB 181000  0           192.168.64.5:51240       111.206.1.10:443  cgroup:/cf/baidunetdisk/sub
\t cubic wscale:7,7 rto:204 rtt:12/6 mss:1448 bytes_sent:300 bytes_retrans:1448 bytes_acked:301 bytes_received:250000 retrans:0/3 rcv_rtt:11 minrtt:10
ESTAB 0       0           [2408:8207::5]:40000     [2408:871a::7]:443 cgroup:/cf/xunlei
\t cubic rto:300 rtt:80.25/10 bytes_acked:90 bytes_received:777
ESTAB 0       0              127.0.0.1:40100          127.0.0.1:9222 cgroup:/cf/xunlei
\t cubic rto:204 rtt:0.05/0.02 bytes_acked:5000 bytes_received:999999
LISTEN 0      128              0.0.0.0:22               0.0.0.0:*    cgroup:/system.slice/ssh.service
SYN-SENT 0    1           192.168.64.5:51300       203.0.113.7:443   cgroup:/cf/baidunetdisk
\t cubic rto:1000 mss:524
ESTAB 0       0           192.168.64.5:39000         1.2.3.4:443    cgroup:/user.slice/user-1000.slice/session-2.scope
\t cubic rtt:5/1 bytes_acked:10 bytes_received:123456
"""

SS_PID = """\
ESTAB 0      0      192.168.64.5:51234  111.206.1.9:443  users:(("baidunetdisk",pid=4100,fd=33))
\t cubic rtt:31.5/4.25 bytes_acked:1201 bytes_received:5000000
ESTAB 0      0      192.168.64.5:52000  58.1.2.3:8080    users:(("thunder",pid=5200,fd=88),("thunder",pid=5201,fd=88))
\t cubic rtt:44/3 bytes_acked:20 bytes_received:4096
ESTAB 0      0      192.168.64.5:53000  9.9.9.9:443
\t cubic rtt:9/3 bytes_acked:20 bytes_received:11
"""


def test_parse_ss_reads_owner_counters_and_round_trip_time():
    socks = N.parse_ss(SS_CGROUP)
    assert [s["state"] for s in socks] == ["ESTAB", "ESTAB", "ESTAB", "ESTAB", "SYN-SENT", "ESTAB"]     # LISTEN is skipped
    a, b, v6, lo, syn, other = socks
    assert a == {"id": "192.168.64.5:51234>111.206.1.9:443", "state": "ESTAB", "peer": "111.206.1.9:443", "recvq": 0,
                 "cgroup": "/cf/baidunetdisk", "pids": [], "rx": 5000000, "tx": 1201, "rtt_ms": 31.5, "rcv_rtt_ms": 30.2,
                 "retr": 0}
    assert b["recvq"] == 181000 and b["retr"] == 3 and b["cgroup"] == "/cf/baidunetdisk/sub" and b["rtt_ms"] == 12
    assert v6["peer"] == "[2408:871a::7]:443" and v6["rx"] == 777 and not N.is_loopback(v6["peer"])
    assert N.is_loopback(lo["peer"]) and N.is_loopback("[::1]:80") and N.is_loopback("[::ffff:127.0.0.1]:80")
    assert syn["rx"] == 0 and syn["rtt_ms"] is None                       # no tcp_info counters yet
    p = N.parse_ss(SS_PID)
    assert [s["pids"] for s in p] == [[4100], [5200, 5201], []] and all(s["cgroup"] is None for s in p)
    # -O prints the tcp_info on the socket's own line
    one = N.parse_ss(SS_CGROUP.replace("\n\t", " "))
    assert [(s["id"], s["rx"]) for s in one] == [(s["id"], s["rx"]) for s in socks]
    assert N.parse_ss("") == [] and N.parse_ss("State Recv-Q Send-Q Local Address:Port Peer Address:Port\n") == []


def test_weighted_median_follows_the_bytes():
    assert N.weighted_median([(10.0, 1), (50.0, 100), (90.0, 1)]) == 50.0
    assert N.weighted_median([(10.0, 0), (50.0, 0), (90.0, 0)]) == 50.0
    assert N.weighted_median([]) is None


class FakeSS:
    """ss whose output is whatever the test sets; refuses --cgroup when `old`."""

    def __init__(self, old=False):
        self.text, self.old, self.calls = "", old, []

    def __call__(self, flags):
        self.calls.append(list(flags))
        if self.old and "--cgroup" in flags:
            raise subprocess.SubprocessError("ss: unrecognized option '--cgroup'")
        return self.text


def sock(local, peer, rx, cgroup=None, pid=None, rtt=20.0, recvq=0, retr=0, tx=100):
    owner = (f' users:(("app",pid={pid},fd=7))' if pid else "") + (f" cgroup:{cgroup}" if cgroup else "")
    return (f"ESTAB {recvq} 0 {local} {peer}{owner}\n\t cubic rtt:{rtt}/1 bytes_acked:{tx} bytes_received:{rx} "
            f"retrans:0/{retr}\n")


def test_sampler_turns_socket_counters_into_per_app_rates():
    ss = FakeSS()
    groups = {"pan": {"cgroup": "/cf/pan", "pids": [10]}, "xl": {"cgroup": "/cf/xl", "pids": [20, 21]}}
    sm = N.SockSampler(lambda: groups, every_s=1.0, run=ss)
    ss.text = (sock("10.0.0.2:1", "1.1.1.1:443", 1000, "/cf/pan") + sock("10.0.0.2:2", "1.1.1.2:443", 500, "/cf/pan/gpu")
               + sock("10.0.0.2:3", "2.2.2.2:80", 70, "/user.slice/x", pid=21)        # started outside, adopted later
               + sock("127.0.0.1:4", "127.0.0.1:9222", 10 ** 6, "/cf/xl")              # the app talking to itself
               + sock("10.0.0.2:5", "3.3.3.3:443", 10 ** 6, "/cf/panda"))              # a different cgroup
    sm.tick(now=100.0)
    assert ss.calls[0] == ["-tinH", "--cgroup", "-p"] and sm.mode == "cgroup+pid"
    assert sm.read("pan") == [] and sm.read("xl") == []                 # the first sample is only the baseline
    ss.text = (sock("10.0.0.2:1", "1.1.1.1:443", 1000 + 3 * 2 ** 20, "/cf/pan", rtt=30, recvq=4096, retr=2)
               + sock("10.0.0.2:2", "1.1.1.2:443", 500 + 2 ** 20, "/cf/pan/gpu", rtt=90)
               + sock("10.0.0.2:9", "1.1.1.3:443", 2 ** 19, "/cf/pan", rtt=10)         # a connection opened since
               + sock("10.0.0.2:3", "2.2.2.2:80", 70, "/user.slice/x", pid=21)
               + sock("127.0.0.1:4", "127.0.0.1:9222", 2 * 10 ** 6, "/cf/xl"))
    sm.tick(now=102.0)
    dl, rtt = sm.read("pan")
    assert dl == {"t": 102.0, "k": "dl", "b": 3 * 2 ** 20 + 2 ** 20 + 2 ** 19, "s": 2.0, "tx": 100, "n": 3, "q": 4096,
                  "retr": 2}
    assert rtt == {"t": 102.0, "k": "rtt", "ms": 30.0, "to": "dl", "n": 3}       # the socket that carried most bytes
    xl = sm.read("xl")
    assert xl == [{"t": 102.0, "k": "dl", "b": 0, "s": 2.0, "tx": 0, "n": 1, "q": 0, "retr": 0}]   # idle: no rtt record
    assert sm.read("pan") == []                                              # read() takes them
    # a 4-tuple that was reused starts from zero; a connection that went away costs nothing
    ss.text = sock("10.0.0.2:1", "1.1.1.1:443", 5000, "/cf/pan")
    sm.tick(now=103.0)
    assert sm.read("pan")[0]["b"] == 5000
    s = N.summarize([dl, rtt])
    assert s["mbps"] == pytest.approx(2.25) and s["rtt_ms"] == 30.0 and s["sockets"] == 3 and s["recvq_kb"] == 4.0


def test_sampler_falls_back_to_the_owning_process_and_can_count_loopback():
    ss = FakeSS(old=True)
    sm = N.SockSampler(lambda: {"xl": {"cgroup": "/cf/xl", "pids": [20]}}, run=ss, loopback=True)
    ss.text = sock("127.0.0.1:4", "127.0.0.1:8000", 100, pid=20) + sock("10.0.0.2:5", "3.3.3.3:443", 100, pid=99)
    sm.tick(now=10.0)
    assert sm.mode == "pid" and sm.flags == ["-tinH", "-p"]
    ss.text = sock("127.0.0.1:4", "127.0.0.1:8000", 2148, pid=20) + sock("10.0.0.2:5", "3.3.3.3:443", 9999, pid=99)
    sm.tick(now=11.0)
    assert sm.read("xl")[0]["b"] == 2048
    with pytest.raises(subprocess.SubprocessError):
        N.ss_flags("cgroup", run=ss)
    assert N.ss_flags("pid", run=ss) == (["-tinH", "-p"], "pid") and N.ss_flags("cgroup", run=FakeSS())[1] == "cgroup"


@pytest.mark.skipif(not shutil.which("ss"), reason="needs ss (iproute2)")
def test_sampler_measures_a_real_download_through_ss():
    srv = B.make_server(0, rate_mbps=4.0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    got = [0]
    stop = threading.Event()

    def fetch():
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/stream", timeout=5) as r:
            while not stop.is_set():
                got[0] += len(r.read(65536))

    sm = N.SockSampler(lambda: {"me": {"cgroup": None, "pids": [os.getpid()]}}, every_s=0.3, loopback=True, by="pid")
    th = threading.Thread(target=fetch, daemon=True)
    try:
        sm.start()
        time.sleep(0.5)                       # the baseline sample is taken before the download starts
        th.start()
        time.sleep(2.5)
        stop.set()
        th.join(5)
        time.sleep(0.5)
        recs = sm.read("me")
    finally:
        sm.stop()
        sm.join(3)
        srv.shutdown()
        srv.server_close()
    assert not sm.error, sm.error
    dl = [r for r in recs if r["k"] == "dl"]
    seen = sum(r["b"] for r in dl)
    # the kernel counted what the client read (+ what was still in its receive queue, + the request on the server side)
    assert got[0] > 2 ** 20 and 0.8 * got[0] < seen < 1.3 * got[0] + 2 ** 20, (got[0], seen)
    busy = [r["b"] / 2 ** 20 / r["s"] for r in dl if r["b"] > 100_000]
    assert len(busy) >= 4 and 2.0 < sorted(busy)[len(busy) // 2] < 6.0, busy          # the source's 4 MB/s cap
    rtt = [r for r in recs if r["k"] == "rtt"]
    assert rtt and all(0 <= r["ms"] < 200 and r["to"] == "dl" for r in rtt)


# ------------------------------------------------------------------ latency probe inside the app's cgroup
def _listener():
    import socket
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(64)
    return srv


def test_net_probe_logs_connect_time_and_wake_up_delay(tmp_path):
    from cf.linux import net_probe as NP
    assert NP.parse_hostport("www.baidu.com:443") == ("www.baidu.com", 443) and NP.parse_hostport("1.2.3.4") == ("1.2.3.4", 443)
    assert NP.parse_hostport("[2408::1]:8443") == ("2408::1", 8443) and NP.parse_hostport("2408::1") == ("2408::1", 443)
    assert NP.CHILD == N.PROBE_CHILD
    srv = _listener()
    port = srv.getsockname()[1]
    log = str(tmp_path / "p.jsonl")
    cg = tmp_path / "cg"
    cg.mkdir()
    try:
        # -S -E and a plain script: nothing of the package (numpy, Xlib ...) is loaded into the app's cgroup
        r = subprocess.run([sys.executable, "-S", "-E", NP.__file__, "--log", log, "--host", f"127.0.0.1:{port}",
                            "--every", "0.1", "--count", "5", "--cgroup", str(cg)], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
    finally:
        srv.close()
    recs = B.read_new(log, 0)[0]
    assert len(recs) == 5 and all(x["k"] == "rtt" and x["to"] == "ext" for x in recs)
    assert all(0 < x["ms"] < 500 and 0 <= x["late"] < 500 for x in recs)
    assert 0.3 < recs[-1]["t"] - recs[0]["t"] < 2.0                          # one probe every 0.1 s
    assert (cg / "cgroup.procs").read_text().isdigit()                       # it moved itself into the given cgroup
    # nothing listens any more: the probe keeps going and logs the loss
    r = subprocess.run([sys.executable, "-S", "-E", NP.__file__, "--log", log, "--host", f"127.0.0.1:{port}",
                        "--every", "0.05", "--count", "2", "--timeout", "0.5"], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and [x["ms"] for x in B.read_new(log, 0)[0][5:]] == [None, None]
    assert subprocess.run([sys.executable, "-S", "-E", NP.__file__], capture_output=True).returncode == 2


@pytest.mark.skipif(not shutil.which("ss"), reason="needs ss (iproute2)")
def test_device_runs_sampler_and_probe_for_a_real_app(tmp_path, monkeypatch):
    from cf.linux import net_probe as NP
    from cf.linux.cgroup import CgroupTree
    from cf.linux.device import LinuxDesktop
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    cat = A.with_overrides({"pan": {"name": "pan", "category": "download", "cmd": "true", "match": "pan", "wm_class": "pan"}})
    dev = LinuxDesktop(cat=cat, tree=CgroupTree(root=str(tmp_path), subdir="cf"), notifications=False)
    srv = _listener()
    try:
        dev.net_start(["pan"], every_s=0.2, loopback=True, by="pid", probe_host=f"127.0.0.1:{srv.getsockname()[1]}",
                      probe_every_s=0.1)
        child = tmp_path / "cf" / "pan" / NP.CHILD
        time.sleep(1.2)
        probe = dev._probes["pan"]
        assert probe.poll() is None and (child / "cgroup.procs").read_text() == str(probe.pid)
        recs = dev.bench_read("pan")
        assert not dev.net.error, dev.net.error
        assert [r for r in recs if r["k"] == "dl"] and len([r for r in recs if r["k"] == "rtt" and r["to"] == "ext"]) >= 5
        time.sleep(0.4)
        again = dev.bench_read("pan")
        assert again and not {r["t"] for r in again} & {r["t"] for r in recs}        # only what is new
    finally:
        dev.close()
        srv.close()
    assert probe.poll() is not None and dev.net is None and dev._probes == {}


# ------------------------------------------------------------------ real tools
def test_corpus_is_built_from_real_files_and_kept(tmp_path):
    src = tmp_path / "src"
    for d, n in (("a", 30), ("b", 30)):
        (src / d).mkdir(parents=True)
        for i in range(n):
            (src / d / f"f{i:02d}.txt").write_bytes(os.urandom(3) * (4000 + 400 * i))
    (src / "a" / "tiny").write_bytes(b"x")                           # too small to be worth packing
    dst = str(tmp_path / "corpus")
    size, n = J.build_corpus(dst, mb=0.5, roots=(str(src / "a"), str(src / "b"), "/nonexistent"))
    assert 0.5 * 2 ** 20 <= size < 1.6 * 2 ** 20 and n == len(os.listdir(dst)) - 1 and n >= 20
    assert not any(f.endswith("tiny") for f in os.listdir(dst))
    assert any("f00.txt" in f for f in os.listdir(dst))
    stamp = os.stat(os.path.join(dst, ".cf_corpus")).st_mtime_ns
    assert J.build_corpus(dst, mb=0.5, roots=("/nonexistent",)) == (size, n)       # reused, not rebuilt
    assert os.stat(os.path.join(dst, ".cf_corpus")).st_mtime_ns == stamp
    # a system without enough files: filled up with generated text
    size2, n2 = J.build_corpus(str(tmp_path / "c2"), mb=1, roots=("/nonexistent",))
    assert size2 >= 2 ** 20 and n2 == 1


def test_smt_problems_are_well_formed():
    for name, gen, answer in J.Z3_PROBLEMS:
        text = gen()
        assert text.startswith("(set-logic ") and text.rstrip().endswith("(check-sat)") and answer in ("sat", "unsat")
        assert text.count("(") == text.count(")")
    assert 65519 * 65521 == int(re.search(r"\(_ bv(\d+) 32\)\)\)\n\(check-sat\)", J.smt_factor()).group(1))


def _fake_tool(tmp_path, body, name="tool"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    p = tmp_path / name
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(0o755)
    return str(p)


def test_tool_worker_times_every_run_and_checks_the_answer(tmp_path):
    tool = _fake_tool(tmp_path, 'case "$1" in good) echo sat;; wrong) echo unsat;; fail) exit 3;; slow) sleep 30;; esac\n')
    log = B.BenchLog(str(tmp_path / "t.jsonl"))
    stop = threading.Event()
    w = J.ToolWorker([J.Step("a", [tool, "good"], expect="sat"), J.Step("b", [tool, "good"])], log, stop)
    w.start()
    while w.done < 3:
        time.sleep(0.02)
    stop.set()
    w.join(5)
    jobs = [r for r in B.read_new(log.path, 0)[0] if r["k"] == "job"]
    assert [r["op"] for r in jobs[:4]] == ["a", "b", "a", "b"] and all(r["ok"] and r["ms"] > 0 and r["cpu"] >= 0 for r in jobs)
    assert set(w.last) == {"a", "b"} and w.bad == 0

    # one record per cycle; a wrong answer or a failing run makes the job bad
    stop = threading.Event()
    w = J.ToolWorker([J.Step("x", [tool, "good"], expect="sat"), J.Step("y", [tool, "wrong"], expect="sat"),
                      J.Step("z", [tool, "fail"])], log, stop, per_op=False)
    n0 = len(B.read_new(log.path, 0)[0])
    w.start()
    while w.done < 2:
        time.sleep(0.02)
    stop.set()
    w.join(5)
    cyc = [r for r in B.read_new(log.path, 0)[0][n0:] if r["k"] == "job"]
    assert cyc and all("op" not in r and r["ok"] is False for r in cyc) and w.bad >= 2

    # shutting down kills the run in progress and logs nothing for it
    stop = threading.Event()
    w = J.ToolWorker([J.Step("s", [tool, "slow"])], log, stop)
    n0 = len(B.read_new(log.path, 0)[0])
    w.start()
    time.sleep(0.5)
    t0 = time.time()
    stop.set()
    w.kill()
    w.join(5)
    assert not w.is_alive() and time.time() - t0 < 3 and len(B.read_new(log.path, 0)[0]) == n0
    log.close()


@pytest.mark.skipif(J.find_tool("sevenzip") is None, reason="needs 7z (p7zip-full)")
def test_real_7zip_packs_and_unpacks_the_corpus(tmp_path):
    work = str(tmp_path)
    size, n = J.build_corpus(os.path.join(work, "corpus"), mb=1)
    log = B.BenchLog(str(tmp_path / "z.jsonl"))
    stop = threading.Event()
    steps = J.sevenzip_steps(J.find_tool("sevenzip"), work)
    w = J.ToolWorker(steps, log, stop)
    (c_ms, c_cpu, c_ok), (x_ms, x_cpu, x_ok) = w._run(steps[0]), w._run(steps[1])      # one cycle, by hand
    assert c_ok and x_ok and c_ms > x_ms > 0 and c_cpu > 0             # packing costs more than unpacking
    out = os.path.join(work, "out", "corpus")
    assert len(os.listdir(out)) == n + 1                               # every file came back (+ the marker)
    assert sum(os.path.getsize(os.path.join(out, f)) for f in os.listdir(out) if not f.startswith(".")) == size
    assert os.path.getsize(os.path.join(work, "a.7z")) < size
    w.start()                                                          # and in a loop: every cycle replaces the archive
    deadline = time.time() + 60
    while w.done < 4 and time.time() < deadline:
        time.sleep(0.05)
    stop.set()
    w.kill()
    w.join(10)
    log.close()
    jobs = [r for r in B.read_new(log.path, 0)[0] if r["k"] == "job"]
    assert [r["op"] for r in jobs[:4]] == ["compress", "extract"] * 2 and all(r["ok"] for r in jobs[:4])


@pytest.mark.skipif(J.find_tool("z3") is None, reason="needs z3")
def test_real_z3_gives_the_known_answers(tmp_path):
    steps = J.z3_steps(J.find_tool("z3"), str(tmp_path))
    assert [s.op for s in steps] == ["queens24", "pigeonhole8", "factor32"]
    for s in steps:
        out = subprocess.run(s.argv, capture_output=True, text=True, timeout=120).stdout.strip()
        assert out == s.expect, (s.op, out)


def test_a_step_can_check_the_output_itself_and_set_the_environment(tmp_path):
    tool = _fake_tool(tmp_path, 'echo "threads=$OMP_THREAD_LIMIT $1"\n')
    log = B.BenchLog(str(tmp_path / "c.jsonl"))
    w = J.ToolWorker([], log, threading.Event())
    seen = []
    ok = J.Step("a", [tool, "x"], env={"OMP_THREAD_LIMIT": "1"}, check=lambda out: seen.append(out) or "threads=1 x" in out)
    assert w._run(ok)[2] is True and seen == ["threads=1 x\n"]
    assert w._run(J.Step("b", [tool, "x"], check=lambda out: "threads=1" in out))[2] is False        # not set: not inherited
    assert w._run(J.Step("c", [tool, "x"], check=lambda out: 1 / 0))[2] is False                     # a check that raises
    fail = _fake_tool(tmp_path / "f", 'echo fine; exit 2\n')
    assert w._run(J.Step("d", [fail], check=lambda out: True))[2] is False                          # exit status first
    log.close()
    errs = [r["msg"] for r in B.read_new(log.path, 0)[0] if r["k"] == "err"]
    assert len(errs) == 3 and errs[0].startswith("b: exit 0, answered 'threads= x'") and errs[2].startswith("d: exit 2")


@pytest.mark.skipif(J.find_tool("openssl") is None, reason="needs openssl")
def test_real_openssl_encrypts_decrypts_and_signs(tmp_path):
    work = str(tmp_path)
    steps = J.openssl_steps(J.find_tool("openssl"), work, mb=2, iters=20000, rsa_bits=2048)
    assert [s.op for s in steps] == ["encrypt", "decrypt", "sign"]
    payload, enc = os.path.join(work, "payload.bin"), os.path.join(work, "payload.enc")
    assert os.path.getsize(payload) == 2 << 20
    log = B.BenchLog(str(tmp_path / "o.jsonl"))
    w = J.ToolWorker(steps, log, threading.Event())
    assert all(w._run(s)[2] for s in steps)
    blob = open(enc, "rb").read()
    assert blob[:8] == b"Salted__" and len(blob) > 2 << 20 and blob[16:4096] != open(payload, "rb").read()[:4080]
    # a damaged ciphertext must not count as a decrypted file, nor a changed file as a signed one
    with open(enc, "r+b") as fh:
        fh.seek(1 << 20)
        fh.write(b"\0" * 64)
    assert w._run(steps[1])[2] is False
    assert w._run(steps[2])[2] is True
    key_before = open(os.path.join(work, "key.pem")).read()
    assert J.openssl_steps(J.find_tool("openssl"), work, mb=2, iters=20000, rsa_bits=2048) and \
        open(os.path.join(work, "key.pem")).read() == key_before                # inputs are kept between runs
    log.close()


def test_ocr_answer_is_judged_by_the_words_it_found():
    words = "The quick brown fox jumps over the lazy dog again and again".split()
    assert J.ocr_ok("the quick brown fox jumps over the lazy dog again and again", words)
    assert J.ocr_ok("The qu1ck brown fox, jumps over the lazy dog again and again.", words)       # 11 of 12
    assert not J.ocr_ok("The qu1ck br0wn f0x jumps over the lazy dog", words)
    assert not J.ocr_ok("", words) and not J.ocr_ok("anything", [])


@pytest.mark.skipif(bool(J.missing("tesseract")), reason="needs tesseract-ocr + python3-pil")
def test_real_tesseract_reads_the_page(tmp_path):
    steps = J.tesseract_steps(J.find_tool("tesseract"), str(tmp_path))
    assert steps[0].env == {"OMP_THREAD_LIMIT": "1"}
    log = B.BenchLog(str(tmp_path / "t.jsonl"))
    w = J.ToolWorker(steps, log, threading.Event())
    ms, cpu, ok = w._run(steps[0])
    assert ok and ms > 50 and cpu <= ms * 1.3                                    # one thread
    from PIL import Image
    Image.new("L", (800, 600), 255).save(os.path.join(str(tmp_path), "page.png"))    # a blank page has no words
    assert w._run(steps[0])[2] is False
    log.close()


@pytest.mark.skipif(J.find_tool("ffmpeg") is None, reason="needs ffmpeg")
def test_real_ffmpeg_transcodes_the_clip(tmp_path):
    work = str(tmp_path)
    steps = J.ffmpeg_steps(J.find_tool("ffmpeg"), work, seconds=1)
    src, out = os.path.join(work, "src.mkv"), os.path.join(work, "out.mp4")
    assert os.path.getsize(src) > 20000
    log = B.BenchLog(str(tmp_path / "f.jsonl"))
    w = J.ToolWorker(steps, log, threading.Event())
    ms, cpu, ok = w._run(steps[0])
    assert ok and cpu > 0 and os.path.getsize(out) > 20000
    probe = subprocess.run([J.find_tool("ffmpeg"), "-hide_banner", "-i", out], capture_output=True, text=True).stderr
    assert "h264" in probe and "960x540" in probe
    os.rename(src, src + ".gone")                                               # no input: no output, not ok
    assert w._run(steps[0])[2] is False and not os.path.exists(out)
    log.close()


def test_llama_binary_and_model_are_looked_up(tmp_path, monkeypatch):
    monkeypatch.setenv("CF_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.delenv("CF_LLAMA_BIN", raising=False)
    monkeypatch.delenv("CF_LLAMA_MODEL", raising=False)
    if shutil.which("llama-completion"):
        pytest.skip("llama-completion is on the system PATH")
    assert J.find_tool("llama") is None and "llama-completion not found" in J.missing("llama")
    exe = _fake_tool(tmp_path / "llama" / "llama.cpp" / "build" / "bin", "echo hi\n", name="llama-completion")
    assert J.find_tool("llama") == exe and "no GGUF model" in J.missing("llama")
    models = tmp_path / "llama" / "models"
    models.mkdir()
    (models / "b.gguf").write_bytes(b"GGUF")
    (models / "a.gguf").write_bytes(b"GGUF")
    assert J.find_model() == str(models / "a.gguf") and J.missing("llama") == ""
    monkeypatch.setenv("CF_LLAMA_MODEL", str(models / "b.gguf"))
    assert J.find_model() == str(models / "b.gguf")
    monkeypatch.setenv("CF_LLAMA_MODEL", str(models / "nope.gguf"))
    assert J.find_model() is None and J.missing("llama")
    monkeypatch.setenv("CF_LLAMA_BIN", str(tmp_path / "nope"))
    assert J.find_tool("llama") is None
    job_env = {"CF_LLAMA_MODEL": str(models / "a.gguf"), "CF_LLAMA_BIN": exe}
    for k, v in job_env.items():
        monkeypatch.setenv(k, v)
    job = J.build("llama", J.find_tool("llama"), str(tmp_path / "w"), tokens=16, threads=3)
    argv = job.steps[0].argv
    assert job.cache == [str(models / "a.gguf")] and job.per_op is False and "16 tokens, 3 threads" in job.what
    assert argv[:3] == [exe, "-m", str(models / "a.gguf")] and argv[argv.index("-n") + 1] == "16" and argv[argv.index("-t") + 1] == "3"
    assert "--ignore-eos" in argv and "--single-turn" in argv and J.LLAMA_PROMPT in argv            # always 16 tokens


def test_llama_job_needs_an_answer_but_not_the_same_one(tmp_path):
    """A busy machine does not always give the same tokens; a model that did not load gives none."""
    tool = _fake_tool(tmp_path, 'n=$(cat "$0.n" 2>/dev/null || echo 0); echo $((n + 1)) > "$0.n"\n'
                                'case $n in 1) echo "另一个回答, 一样是六十四个 token 的工作量";; 2) echo;; 3) echo "短";;\n'
                                '*) echo "操作系统把长时间不用的内存页换出到磁盘, 把内存让给别的程序";; esac\n')
    st = J.llama_steps(tool, "/nonexistent/model.gguf")[0]
    assert st.argv[st.argv.index("-t") + 1] == "1"                              # see llama_steps: busy-waiting threads
    log = B.BenchLog(str(tmp_path / "l.jsonl"))
    w = J.ToolWorker([st], log, threading.Event())
    assert [w._run(st)[2] for _ in range(5)] == [True, True, False, False, True]
    log.close()


@pytest.mark.skipif(bool(J.missing("llama")), reason="needs llama.cpp + a GGUF model (install_compute_apps.sh --llama)")
def test_real_llama_generates_the_tokens_it_was_asked_for(tmp_path):
    job = J.build("llama", J.find_tool("llama"), str(tmp_path), tokens=24)
    log = B.BenchLog(str(tmp_path / "l.jsonl"))
    w = J.ToolWorker(job.steps, log, threading.Event())
    (ms, cpu, ok), (_, _, again) = w._run(job.steps[0]), w._run(job.steps[0])
    log.close()
    assert ok and again and cpu > 100, [r for r in B.read_new(log.path, 0)[0] if r["k"] == "err"]
    err = subprocess.run(job.steps[0].argv, capture_output=True, stdin=subprocess.DEVNULL).stderr.decode()
    assert re.search(r"eval time =.*/\s+23 runs", err), err[-600:]              # 24 tokens: the first comes with the prompt


def test_evict_drops_a_file_from_the_page_cache(tmp_path):
    J.evict(str(tmp_path / "missing"))                                          # nothing to do, no error
    f = tmp_path / "big"
    f.write_bytes(os.urandom(1 << 20))
    os.sync()
    J.evict(str(f))
    assert f.read_bytes()[:4] is not None


def test_check_runs_a_cycle_and_reports_what_is_missing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(J, "find_tool", lambda kind: None)
    assert J.check("z3")[0] is False and "sudo apt install z3" in J.check("z3")[1]
    assert J.main(["sevenzip", "llama"]) == 1
    out = capsys.readouterr().out
    assert "sevenzip   MISSING" in out and "install_compute_apps.sh --llama" in out
    assert J.main(["nosuchkind"]) == 2
    monkeypatch.undo()
    tool = _fake_tool(tmp_path, 'case "$1" in *queens*|*factor*) echo sat;; *) echo unsat;; esac\n')
    monkeypatch.setattr(J, "find_tool", lambda kind: tool)
    ok, line = J.check("z3", cycles=2, work=str(tmp_path / "w"))
    assert ok and "queens24" in line and "factor32" in line and "cpu" in line
    wrong = _fake_tool(tmp_path / "w2", "echo sat\n")
    monkeypatch.setattr(J, "find_tool", lambda kind: wrong)
    ok, line = J.check("z3", work=str(tmp_path / "w"))
    assert not ok and "pigeonhole8: exit 0, answered 'sat'" in line


def test_missing_tools_of_a_config_are_listed(tmp_path, monkeypatch, capsys):
    cfg = os.path.join(REPO, "configs", "ubuntu_arm64_compute.json")
    assert J.config_kinds(cfg) == {k: k for k in ("sevenzip", "z3", "openssl", "llama", "tesseract", "ffmpeg")}
    assert J.config_kinds(os.path.join(REPO, "configs", "ubuntu_arm64_classes_synthetic.json")) == {}
    monkeypatch.setattr(J, "missing", lambda kind, *a, **kw: "no model" if kind == "llama" else "")
    assert J.main(["--missing", cfg]) == 1
    assert capsys.readouterr().out.strip() == "llama: no model  ->  scripts/ubuntu/install_compute_apps.sh --llama"
    monkeypatch.setattr(J, "missing", lambda kind, *a, **kw: "")
    assert J.main(["--missing", cfg]) == 0 and capsys.readouterr().out == ""


def test_real_tool_apps_are_in_the_catalogue(monkeypatch):
    from cf.linux.onboard import login_spec
    from cf.linux.work import default_spec
    for id_, cmd in (("sevenzip", "7z"), ("z3", "z3")):
        a = A.CATALOG[id_]
        assert a.kind == "bench" and a.category == "compute" and f"--kind {id_}" in a.cmd and "apt install" in a.install
        assert re.search(a.match, f"python3 -m cf.linux.bench_app --app {id_} --kind {id_}")
        assert default_spec(a) == {"kind": "none", "oracle": "statefile"} and login_spec(a)["method"] == "none"
        # installed = the window can be drawn and the tool is on PATH
        monkeypatch.setattr(J, "find_tool", lambda kind: None)
        assert A.is_installed(a) is False
        monkeypatch.setattr(J, "find_tool", lambda kind: "/usr/bin/" + kind)
        import importlib.util
        assert A.is_installed(a) is (importlib.util.find_spec("Xlib") is not None)
    assert J.TOOLS["sevenzip"][0] == "7z" and all(k in J.INSTALL for k in J.TOOLS)
    monkeypatch.undo()
    for id_ in ("openssl", "llama", "tesseract", "ffmpeg"):
        a = A.CATALOG[id_]
        assert a.kind == "bench" and a.category == "compute" and f"--kind {id_}" in a.cmd and id_ in B.TOOL_KINDS
        assert re.search(a.match, f"python3 -m cf.linux.bench_app --app {id_} --kind {id_}")
        assert default_spec(a) == {"kind": "none", "oracle": "statefile"} and login_spec(a)["method"] == "none"
        import shlex
        assert shlex.split(a.cmd)[-1] == a.name                                 # the title survives the shell
    # a language model needs its model file as well as its program
    monkeypatch.setattr(J, "find_tool", lambda kind: "/usr/bin/" + kind)
    monkeypatch.setattr(J, "find_model", lambda: None)
    assert A.is_installed(A.CATALOG["llama"]) is False
    assert set(B.TOOL_KINDS) == set(J.TOOLS) and set(J.THREADS) <= set(J.TOOLS)


def test_tool_runs_are_not_the_apps_state():
    """7z / z3 are started anew for every job: a run that ended is not a lost process."""
    from cf.linux import procs as PR
    from cf.linux.device import AppSampleL, LinuxDesktop
    win = PR.ProcInfo(pid=10, name="python3", cmdline="python3 -m cf.linux.bench_app --app sevenzip --kind sevenzip", age_s=300)
    run1 = PR.ProcInfo(pid=11, name="7z", cmdline="7z a -mx=5 a.7z corpus", age_s=40)       # a slow, limited run
    run2 = PR.ProcInfo(pid=12, name="7z", cmdline="7z a -mx=5 a.7z corpus", age_s=1)
    before = AppSampleL("sevenzip", [win, run1], window=7, state_pids=[10]).to_dict()
    after = AppSampleL("sevenzip", [win, run2], window=7, state_pids=[10]).to_dict()
    assert before["pids"] == [10, 11] and before["stable_pids"] == [10]
    assert LinuxDesktop.state_check(before, after) == {}
    gone = AppSampleL("sevenzip", [run2], window=7, state_pids=[]).to_dict()
    assert LinuxDesktop.state_check(before, gone) == {"processes_gone": [10]}               # the window itself is state
    # any other app: every process that has lived for a while counts
    plain = AppSampleL("wps", [win, run1], window=7).to_dict()
    assert plain["stable_pids"] == [10, 11]


def test_configs_real_and_synthetic():
    real = json.load(open(os.path.join(REPO, "configs", "ubuntu_arm64_classes.json")))
    syn = json.load(open(os.path.join(REPO, "configs", "ubuntu_arm64_classes_synthetic.json")))
    for cfg in (real, syn):
        apps, cl, pat = set(cfg["apps"]), cfg["classes"], cfg["trace"]["pattern"]
        assert set(cl["interactive"]) | set(cl["compute"]) | set(cl["download"]) == apps == set(pat)
        assert all(pat[i] != pat[i - 1] for i in range(len(pat))) and cfg["trace"]["T"] % len(pat) == 0
        assert all(pat.count(a) == 1 for a in cl["compute"] + cl["download"])
        assert all(pat.count(a) >= 2 for a in cl["interactive"]) and all(a in A.CATALOG for a in apps)
    assert real["classes"]["compute"] == ["sevenzip", "z3"] and real["classes"]["download"] == ["baidunetdisk", "xunlei"]
    assert all(A.CATALOG[a].kind == "desktop" and A.CATALOG[a].category == "download" for a in real["classes"]["download"])
    assert real["net_probe"]["by"] == "auto" and real["net_probe"]["loopback"] is False
    assert all(A.CATALOG[a].kind == "bench" for a in syn["classes"]["compute"] + syn["classes"]["download"])
    assert real["throttle"] == syn["throttle"] and real["dwell_s"] == syn["dwell_s"]
    # responsiveness + compute only: the same interactive apps and limits, six real tools, no download app
    comp = json.load(open(os.path.join(REPO, "configs", "ubuntu_arm64_compute.json")))
    apps, cl, pat = set(comp["apps"]), comp["classes"], comp["trace"]["pattern"]
    assert set(cl["interactive"]) | set(cl["compute"]) == apps == set(pat) and cl["download"] == []
    assert cl["interactive"] == real["classes"]["interactive"]
    assert cl["compute"] == ["sevenzip", "z3", "openssl", "llama", "tesseract", "ffmpeg"]
    assert all(pat[i] != pat[i - 1] for i in range(len(pat))) and comp["trace"]["T"] % len(pat) == 0
    assert all(pat.count(a) == 1 for a in cl["compute"]) and all(pat.count(a) == 2 for a in cl["interactive"])
    assert {k: v for k, v in comp["throttle"].items() if k != "_comment"} == \
        {k: v for k, v in real["throttle"].items() if k != "_comment"} and comp["dwell_s"] == real["dwell_s"]


# ------------------------------------------------------------------ runner + analyzer on the fake desktop
INTER = ["fake_office", "fake_chat"]
REAL_OV = {"fake_zip": {"name": "假7z", "category": "compute", "kind": "bench", "cmd": "true", "match": "fake_zip",
                        "wm_class": "fake_zip", "arch": ["any"]},
           "fake_smt": {"name": "假求解器", "category": "compute", "kind": "bench", "cmd": "true", "match": "fake_smt",
                        "wm_class": "fake_smt", "arch": ["any"]}}
APPS = INTER + ["fake_zip", "fake_smt", "fake_browser"]            # fake_browser: a real (non-bench) download app
PATTERN = ["fake_office", "fake_zip", "fake_chat", "fake_browser", "fake_office", "fake_smt", "fake_chat"]


def run_fake(tmp_path, policy, name):
    cfg = {"apps": APPS, "app_overrides": {**{a: OVERRIDES[a] for a in INTER + ["fake_browser"]}, **REAL_OV},
           "policy": policy, "eta": 0.0, "trace": {"kind": "pattern", "pattern": PATTERN, "T": 14, "seed": 1},
           "dwell_s": 0.3, "settle_s": 0, "warmup_dwell_s": 0, "work": True, "work_s": 0.01,
           "classes": {"interactive": INTER, "compute": ["fake_zip", "fake_smt"], "download": ["fake_browser"]},
           "net_probe": {"every_s": 0.5, "loopback": True, "probe_host": "probe.example:443"},
           "throttle": {"cpu_cores": 0.25, "mem_frac": 0.1, "mem_min_mb": 32, "squeeze_rounds": 1, "thrash_high_per_s": 0},
           "out_dir": str(tmp_path), "name": name, "min_ram_mb": 1024}
    os.environ.pop("CF_BENCH_RTT_HOST", None)
    dev = FakeLinuxDesktop(APPS, seed=3)
    dev.bench_ops["fake_zip"] = [("compress", 2.0, 1.8), ("extract", 0.5, 1.0)]
    dev.work_driver = FakeWorkDriver(dev, cfg)
    path = ThrottleExperiment(cfg, dev, log=lambda *a: None).run()
    return load(path), dev


@pytest.fixture(scope="module")
def real_runs(tmp_path_factory):
    d = tmp_path_factory.mktemp("real")
    (none, dev), (sq, _) = run_fake(d, "none", "none_classes_s1"), run_fake(d, "static", "squeeze_classes_s1")
    return d, none, sq, dev


def test_a_run_starts_without_limits_left_by_an_earlier_one(tmp_path):
    """A crashed run (or, before the cpuset fix, any run that pinned apps to one core) leaves limits
    behind; a policy-none run never changes a tier, so it would measure them: every limit is lifted
    before the first app is brought up."""
    cfg = {"apps": APPS, "app_overrides": {**{a: OVERRIDES[a] for a in INTER + ["fake_browser"]}, **REAL_OV},
           "policy": "none", "eta": 0.0, "trace": {"kind": "pattern", "pattern": PATTERN, "T": 3, "seed": 1},
           "dwell_s": 0.1, "settle_s": 0, "warmup_dwell_s": 0, "out_dir": str(tmp_path), "name": "stale",
           "min_ram_mb": 1024}
    dev = FakeLinuxDesktop(APPS, seed=3)
    dev.tiers["fake_smt"] = "bg"                                     # what the earlier run left
    seen = []
    release = dev.release_all

    def spy():
        seen.append((dev.launches, dict(dev.tiers)))
        return release()

    dev.release_all = spy
    ThrottleExperiment(cfg, dev, log=lambda *a: None).run()
    assert seen and seen[0] == (0, {"fake_smt": "bg"})               # before anything was launched


def test_a_limit_the_kernel_refused_is_reported(tmp_path):
    """The foreground app's limits are lifted outside the step's action list; a refused write there
    (an app left pinned to the background's core) used to pass without a word."""
    cfg = {"apps": APPS, "app_overrides": {**{a: OVERRIDES[a] for a in INTER + ["fake_browser"]}, **REAL_OV},
           "policy": "static", "eta": 0.0, "trace": {"kind": "pattern", "pattern": PATTERN, "T": 6, "seed": 1},
           "dwell_s": 0.1, "settle_s": 0, "warmup_dwell_s": 0, "out_dir": str(tmp_path), "name": "refused",
           "min_ram_mb": 1024, "throttle": {"cpu_mode": "both", "cpu_cores": 0.1}}
    dev = FakeLinuxDesktop(APPS, seed=3)
    set_tier = dev.set_tier

    def refusing(id_, tier, *a, **kw):
        r = set_tier(id_, tier, *a, **kw)
        if tier == "fg":
            r["limits"] = {**r["limits"], "cpuset": "ERR ENOSPC No space left on device"}
        return r

    dev.set_tier = refusing
    lines = []
    ThrottleExperiment(cfg, dev, log=lambda *a: lines.append(" ".join(str(x) for x in a))).run()
    hits = [ln for ln in lines if "was not applied" in ln and "ENOSPC" in ln]
    assert hits and all("tier fg" in ln for ln in hits)
    assert len(hits) == len(set(hits))                                    # once per app and knob


def _short_cfg(tmp_path, name, policy="static"):
    return {"apps": APPS, "app_overrides": {**{a: OVERRIDES[a] for a in INTER + ["fake_browser"]}, **REAL_OV},
            "policy": policy, "eta": 0.0, "trace": {"kind": "pattern", "pattern": PATTERN, "T": 3, "seed": 1},
            "dwell_s": 0.1, "settle_s": 0, "warmup_dwell_s": 0, "out_dir": str(tmp_path), "name": name,
            "min_ram_mb": 1024}


def test_processes_that_left_the_cgroup_at_launch_are_moved_back_before_m_fg_is_taken(tmp_path):
    """A launcher that hands the real program to a snap / desktop scope leaves the app's cgroup with
    a shell in it: m_fg came out as 0 and the limits reached nothing until the next run adopted
    the processes - too late for their memory, which stays charged where it was allocated."""
    dev = FakeLinuxDesktop(APPS, seed=3)
    order = []
    launch, sample = dev.launch, dev.sample

    def spy_launch(id_):
        order.append(("launch", id_))
        return launch(id_)

    def spy_sample(ids, *a, **kw):
        order.append(("sample", None))
        return sample(ids, *a, **kw)

    def adopt(id_):
        escaped = id_ == "fake_browser" and order[-1] == ("launch", id_) and order.count(("launch", id_)) == 1
        order.append(("adopt", id_))
        return {"moved": [4242, 4243] if escaped else [], "failed": {}}

    dev.launch, dev.sample, dev.adopt = spy_launch, spy_sample, adopt
    lines = []
    ThrottleExperiment(_short_cfg(tmp_path, "escaped"), dev, log=lambda *a: lines.append(" ".join(str(x) for x in a))).run()
    i = order.index(("launch", "fake_browser"))
    assert order[i + 1] == ("adopt", "fake_browser")                     # at once: before the work, the dwell, the sample
    assert ("sample", None) in order[i + 2:]
    hits = [ln for ln in lines if "had left its cgroup" in ln]
    assert len(hits) == 1 and "2 process(es)" in hits[0] and "[4242, 4243]" in hits[0]


def test_m_fg_of_a_tool_app_is_the_peak_of_its_cgroup(tmp_path):
    """The tool is started for every job: a single sample catches it between two jobs or in the
    middle of one.  memory.high must be a fraction of what the job needs."""
    dev = FakeLinuxDesktop(APPS, seed=3)
    plain = load(ThrottleExperiment(_short_cfg(tmp_path, "nopeak"), FakeLinuxDesktop(APPS, seed=3), log=lambda *a: None).run())
    dev.mem_peak = {"fake_zip": 480 * 1024, "fake_smt": 1, "fake_office": 9 * 1024 * 1024}
    run = load(ThrottleExperiment(_short_cfg(tmp_path, "peak"), dev, log=lambda *a: None).run())
    m, m0 = run["budget"]["m_fg_kb"], plain["budget"]["m_fg_kb"]
    assert m["fake_zip"] == 480 * 1024 > m0["fake_zip"]
    assert m["fake_smt"] == m0["fake_smt"]                                # a peak below the sample changes nothing
    assert m["fake_office"] == m0["fake_office"]                          # only for apps that start a tool per job
    assert run["budget"]["a_fg_kb"]["fake_zip"] == 480 * 1024


def test_m_fg_taken_right_after_a_cold_start_is_called_out(tmp_path):
    lines = []
    ThrottleExperiment(_short_cfg(tmp_path, "coldstart"), FakeLinuxDesktop(APPS, seed=3),
                       log=lambda *a: lines.append(" ".join(str(x) for x in a))).run()
    hits = [ln for ln in lines if "were started by this warmup" in ln]
    assert len(hits) == 1 and "--launch" in hits[0]
    assert "假7z" not in hits[0]                                          # bench apps always start with the run

    dev = FakeLinuxDesktop(APPS, seed=3)
    exp = ThrottleExperiment(_short_cfg(tmp_path, "running"), dev, log=lambda *a: lines.append(" ".join(str(x) for x in a)))
    for a in INTER + ["fake_browser"]:                                    # already up before the run: nothing to say
        dev.launch(a)
    lines.clear()
    exp.run()
    assert not [ln for ln in lines if "were started by this warmup" in ln]


def test_report_flags_an_app_whose_memory_limit_did_not_bite(real_runs):
    """Moved into its cgroup after it had allocated its memory, an app keeps all of it while 'shrunk'."""
    _, none, sq, _ = real_runs
    assert C.mem_unbounded(none) == [] and C.mem_unbounded(sq) == [] and "内存限制在这一格没有生效" not in C.report_md([none, sq])
    odd = json.loads(json.dumps({k: sq[k] for k in ("meta", "steps", "path")}))
    n = 0
    for s in odd["steps"]:
        a = s["after_dwell"]["apps"]["fake_office"]
        if s["req"] != "fake_office" and a.get("tier") == "shrunk":
            a.update(pss_anon_kb=540 * 1024, cg_mem_mb=6.0)
            a["cg_limits"] = {**(a.get("cg_limits") or {}), "memory_high": str(32 << 20)}
            n += 1
    assert n >= 2
    assert C.mem_unbounded(odd) == [("fake_office", 540, 32, 6)]
    rep = C.report_md([none, odd])
    assert "的内存限制在这一格没有生效" in rep and "约 540 MB 匿名内存 (memory.high = 32 MB, 它的 cgroup 只记着 6 MB)" in rep


def test_figure_has_a_row_per_class_that_has_data(real_runs, tmp_path, monkeypatch):
    import matplotlib.pyplot as plt
    _, none, sq, _ = real_runs
    seen = []
    real = plt.subplots
    monkeypatch.setattr(plt, "subplots", lambda *a, **kw: seen.append(a[:2]) or real(*a, **kw))
    tb = C.tables([none, sq])
    assert C.plot_classes([none, sq], str(tmp_path / "all.png"), tb) and seen[-1] == (3, 2)
    assert C.plot_classes([none, sq], str(tmp_path / "two.png"), dict(tb, download=[])) and seen[-1] == (2, 2)
    assert os.path.getsize(tmp_path / "two.png") > 10000


def test_runner_samples_the_sockets_of_real_download_apps(real_runs):
    _, none, sq, dev = real_runs
    # only the real download app is watched (bench apps log for themselves), with the config's settings
    assert dev.net_calls == [{"ids": ["fake_browser"], "every_s": 0.5, "loopback": True, "by": "auto",
                              "probe_host": "probe.example:443"}]
    assert dev.net_apps == []                                             # stopped with the run
    assert none["meta"]["kinds"]["fake_browser"] == "desktop"
    for s in sq["steps"]:
        assert set(s["bench"]) == {"fake_zip", "fake_smt", "fake_browser"}
        recs = s["bench"]["fake_browser"]
        assert {r["k"] for r in recs} == {"dl", "rtt"} and all("t" not in r and "dt" in r for r in recs)
        assert {r["to"] for r in recs if r["k"] == "rtt"} == {"dl", "ext"}
        assert {r.get("op") for r in s["bench"]["fake_zip"]} <= {"compress", "extract"}
        assert all("op" not in r for r in s["bench"]["fake_smt"])


def test_compute_rows_split_by_operation_and_download_rows_use_socket_figures(real_runs):
    _, none, sq, _ = real_runs
    tb = C.tables([none, sq])
    comp = {(r["run"], r["app"]): r for r in tb["compute"]}
    assert {a for _, a in comp} == {"fake_zip:compress", "fake_zip:extract", "fake_smt"}
    n, s = comp[("none_classes_s1", "fake_zip:compress")], comp[("squeeze_classes_s1", "fake_zip:compress")]
    assert n["op"] == "compress" and n["fg_job_p50_ms"] == pytest.approx(8.0, rel=0.05)
    assert s["bg_job_p50_ms"] == pytest.approx(32.0, rel=0.05) and s["slowdown_x"] == pytest.approx(4.0, rel=0.05)
    assert s["bg_vs_none_x"] == pytest.approx(4.0, rel=0.05) and n["bg_vs_none_x"] == pytest.approx(1.0)
    assert n["fg_cpu_pct"] == pytest.approx(180, abs=3) and s["bg_cpu_pct"] == pytest.approx(45, abs=3)   # two threads
    x = comp[("squeeze_classes_s1", "fake_zip:extract")]
    assert x["op"] == "extract" and x["fg_job_p50_ms"] == pytest.approx(2.0, rel=0.05)
    assert x["bg_job_p50_ms"] == pytest.approx(8.0, rel=0.05)
    # compress and extract alternate: the same number of each per minute
    assert abs(x["bg_jobs"] - s["bg_jobs"]) <= 2 and comp[("squeeze_classes_s1", "fake_smt")]["op"] == ""
    assert comp[("squeeze_classes_s1", "fake_smt")]["slowdown_x"] == pytest.approx(4.0, rel=0.05)

    dl = {r["run"]: r for r in tb["download"]}
    n, s = dl["none_classes_s1"], dl["squeeze_classes_s1"]
    assert n["app"] == "fake_browser" and n["source"] == s["source"] == "sockets"
    assert n["fg_mbps"] == pytest.approx(3.0, rel=0.05) and n["bg_mbps"] == pytest.approx(3.0, rel=0.05)
    assert s["bg_mbps"] == pytest.approx(0.75, rel=0.05) and s["bg_speed_pct"] == pytest.approx(25, abs=3)
    assert s["bg_vs_none_pct"] == pytest.approx(25, abs=3)
    # the path's round trip does not change; the backlog in the kernel's receive queue does
    assert s["fg_rtt_p50_ms"] == s["bg_rtt_p50_ms"] == n["bg_rtt_p50_ms"] == 23.5
    assert n["bg_recvq_p95_kb"] == pytest.approx(19.5, abs=0.1) and s["bg_recvq_p95_kb"] == pytest.approx(878.9, abs=0.1)
    assert s["fg_recvq_p95_kb"] == pytest.approx(19.5, abs=0.1) and s["bg_socks"] == 4 and s["bg_retrans"] == 0
    assert s["bg_idle_pct"] == 0 and s["fg_idle_pct"] == 0
    # the probe inside the app's cgroup: it waits for the CPU like the app's own threads do
    assert n["bg_wake_p50_ms"] == 0 and s["fg_wake_p50_ms"] == 0 and s["bg_wake_p50_ms"] == 7.5
    assert n["bg_ext_rtt_p50_ms"] == s["fg_ext_rtt_p50_ms"] == 31.0 and s["bg_ext_rtt_p50_ms"] == 38.5
    assert s["errors"] == 0 and s["rtt_lost_pct"] == 0
    assert s["bg_disk_mbps"] is not None and s["fg_disk_mbps"] is not None


def test_idle_seconds_flag_a_download_that_ended(real_runs):
    _, none, _, _ = real_runs
    run = json.loads(json.dumps({k: none[k] for k in ("meta", "steps", "path")}))
    half = len(run["steps"]) // 2
    for s in run["steps"][half:]:
        for r in s["bench"]["fake_browser"]:
            if r["k"] == "dl":
                r["b"] = 0
        s["bench"]["fake_browser"] = [r for r in s["bench"]["fake_browser"] if r["k"] != "rtt"]
    row = C.download_rows(run)[0]
    assert 30 < row["bg_idle_pct"] < 70 and row["bg_mbps"] < 2.0


def test_report_flags_a_foreground_that_was_still_pinned(real_runs):
    """Result files from before the cpuset fix show it in the foreground app's own limits."""
    _, none, sq, _ = real_runs
    assert C.fg_pinned(sq) == (0, "") and "钉在核" not in C.report_md([none, sq])
    old = json.loads(json.dumps({k: sq[k] for k in ("meta", "steps", "path")}))
    old["meta"]["caps"]["effective_cpus"] = list(range(8))
    for s in old["steps"][:2]:                                          # unpinned: all eight cores
        s["after_dwell"]["apps"][s["req"]]["cg_limits"] = {"cpu_max": "max 100000", "cpuset": "0-7", "memory_high": "max"}
    for s in old["steps"][2:]:
        s["after_dwell"]["apps"][s["req"]]["cg_limits"] = {"cpu_max": "max 100000", "cpuset": "7", "memory_high": "max"}
    assert C.fg_pinned(old) == (len(old["steps"]) - 2, "7")
    assert f"{len(old['steps']) - 2}/{len(old['steps'])} 步里前台应用仍被 cpuset 钉在核 7 上" in C.report_md([none, old])


def test_report_names_operations_and_hides_empty_columns(real_runs, capsys):
    d, none, sq, _ = real_runs
    out = d / "out"
    assert C.main([none["path"], sq["path"], "--out", str(out)]) == 0
    text = capsys.readouterr().out
    assert "fake_zip · 压缩" in text and "fake_zip · 解压" in text and "| fake_smt |" in text
    dl_head = next(line for line in text.splitlines() if line.startswith("| 格 | 应用 | 前台 下载速度"))
    assert "内核收包积压" in dl_head and "写盘" in dl_head and "处理延迟" in dl_head and "外网时延" in dl_head
    rows = list(csv.DictReader(open(out / "classes_compute.csv")))
    assert [r["app"] for r in rows[:3]] == ["fake_zip:compress", "fake_zip:extract", "fake_smt"] and rows[0]["op"] == "compress"
    rows = list(csv.DictReader(open(out / "classes_download.csv")))
    assert rows[1]["source"] == "sockets" and float(rows[1]["bg_recvq_p95_kb"]) > 800
    assert C._cn("sevenzip:compress") == "7-Zip · 压缩" and C._cn("z3") == "Z3 求解器" and C._cn("ALL") == "全部交互应用"
    pytest.importorskip("matplotlib")
    for f in ("classes.png", "none_classes_s1_bench.png", "squeeze_classes_s1_bench.png"):
        assert (out / "plots" / f).stat().st_size > 10000


# ------------------------------------------------------------------ the real window (Xvfb) with the real tools
def _xvfb_available():
    import importlib.util
    return bool(shutil.which("Xvfb")) and importlib.util.find_spec("Xlib") is not None


@pytest.mark.skipif(not _xvfb_available(), reason="needs Xvfb + python3-xlib")
@pytest.mark.parametrize("kind,ops,need", [("sevenzip", {"compress", "extract"}, 4), ("z3", {None}, 4),
                                           ("openssl", {"encrypt", "decrypt", "sign"}, 6), ("tesseract", {None}, 2),
                                           ("ffmpeg", {None}, 2), ("llama", {None}, 2)])
def test_bench_app_window_drives_the_real_tool(tmp_path, kind, ops, need):
    if J.missing(kind):
        pytest.skip(f"{kind}: {J.missing(kind)}")
    disp = ":%d" % (190 + os.getpid() % 40)
    xvfb = subprocess.Popen(["Xvfb", disp, "-screen", "0", "800x600x24"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    env = dict(os.environ, DISPLAY=disp, PYTHONPATH=REPO)
    log, state, work = str(tmp_path / "b.jsonl"), str(tmp_path / "b.json"), str(tmp_path / "work")
    proc = None
    try:
        sock, t_up = "/tmp/.X11-unix/X" + disp[1:], time.time() + 15
        while not os.path.exists(sock) and time.time() < t_up:
            time.sleep(0.1)
        time.sleep(0.3)
        proc = subprocess.Popen([sys.executable, "-m", "cf.linux.bench_app", "--app", "t_" + kind, "--kind", kind,
                                 "--corpus-mb", "1", "--log", log, "--state-file", state, "--work-dir", work], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        deadline = time.time() + 90
        jobs = []
        while time.time() < deadline and (len(jobs) < need or not os.path.exists(state)):
            time.sleep(0.2)
            assert proc.poll() is None, proc.stderr.read().decode()
            jobs = [r for r in B.read_new(log, 0)[0] if r["k"] == "job"]
        assert len(jobs) >= need and all(r["ok"] for r in jobs) and {r.get("op") for r in jobs} == ops
        st = json.load(open(state))
        assert st["pid"] == proc.pid and st["kind"] == kind
        start = B.read_new(log, 0)[0][0]
        assert start["k"] == "start" and os.path.basename(J.find_tool(kind)) in start["what"]
        proc.terminate()
        proc.wait(10)
        time.sleep(0.3)
        left = subprocess.run(["pgrep", "-f", work], capture_output=True, text=True).stdout.split()
        assert left == []                                              # the tool does not outlive the window
    finally:
        if proc and proc.poll() is None:
            proc.kill()
        xvfb.terminate()
        xvfb.wait(10)                          # the next case uses the same display number


def test_operations_are_listed_in_a_fixed_order():
    """A log that happens to start with 'decrypt' (the first job was cut by the warmup) must not
    reorder the OpenSSL rows of that cell."""
    ev = [{"op": "decrypt"}, {"op": "sign"}, {"op": "mystery"}, {"op": "encrypt"}, {"op": "decrypt"}]
    assert C._ops(ev) == ["encrypt", "decrypt", "sign", "mystery"]
    assert C._ops([{"k": "job"}, {"op": ""}]) == [""]
    assert C._ops([]) == []
