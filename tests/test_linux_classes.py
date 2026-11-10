"""Three-class experiment (manual §6.9): the bundled bench apps (kernels, job log, download source +
client + connect probe), the `pattern` trace, the runner hooks (bench records per step, the
responsiveness probe) and the cf.linux.classes tables / report / figures."""
import csv
import json
import os
import subprocess
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from fake_linux_desktop import BENCH_OVERRIDES, OVERRIDES, FakeLinuxDesktop, FakeWorkDriver  # noqa: E402

from cf.analyze import load  # noqa: E402
from cf.linux import apps as A  # noqa: E402
from cf.linux import bench_app as B  # noqa: E402
from cf.linux import classes as C  # noqa: E402
from cf.linux.runner import ThrottleExperiment  # noqa: E402
from cf.trace import make_trace  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ------------------------------------------------------------------ bench app pieces
def test_kernels_verify_their_own_results():
    assert B.k_lu(24) < 1e-9 and B.k_fft(8) < 1e-9 and B.k_nbody(200) < 1e-4
    assert B.numeric_job(0.01) is True
    corpus = B.make_corpus(2)
    assert len(corpus) == 2 * 2 ** 20
    assert B.compress_job(corpus, 0, slice_mb=1) is True and B.compress_job(corpus, 5, slice_mb=1) is True
    # compressible like real data, not random noise and not zeros
    import zlib
    ratio = len(zlib.compress(corpus[:2 ** 20], 6)) / 2 ** 20
    assert 0.1 < ratio < 0.9, ratio


def test_job_log_is_read_incrementally(tmp_path):
    p = str(tmp_path / "bench.jsonl")
    assert B.read_new(p, 0) == ([], 0)                     # not started yet
    log = B.BenchLog(p)
    log.put("job", ms=12.5, cpu=12.0, w=0, ok=True)
    recs, off = B.read_new(p, 0)
    assert [r["k"] for r in recs] == ["job"] and recs[0]["ms"] == 12.5 and recs[0]["t"] > 1e9
    assert B.read_new(p, off) == ([], off)
    log.put("dl", b=1000, s=1.0)
    with open(p, "a") as f:
        f.write('{"t": 1, "k": "jo')                       # a line still being written
    recs, off2 = B.read_new(p, off)
    assert [r["k"] for r in recs] == ["dl"] and off2 > off
    with open(p, "a") as f:
        f.write('b"}\n')
    assert [r["k"] for r in B.read_new(p, off2)[0]] == ["job"]
    log.close()
    log = B.BenchLog(p)                                    # the app restarted: the file starts over
    log.put("job", ms=1, cpu=1, w=0, ok=True)
    recs, off3 = B.read_new(p, off2)
    assert len(recs) == 1 and off3 < off2
    log.close()


def test_job_worker_logs_wall_and_cpu_time(tmp_path):
    log = B.BenchLog(str(tmp_path / "w.jsonl"))
    stop = threading.Event()
    w = B.JobWorker(0, lambda n: B.numeric_job(0.01), log, stop)
    w.start()
    time.sleep(0.3)
    stop.set()
    w.join(5)
    log.close()
    recs, _ = B.read_new(log.path, 0)
    jobs = [r for r in recs if r["k"] == "job"]
    assert len(jobs) >= 3 and w.done == len(jobs) and w.bad == 0
    assert all(r["ok"] and r["ms"] > 0 and 0 < r["cpu"] <= r["ms"] * 1.5 for r in jobs)


def test_download_source_client_and_rtt_probe(tmp_path):
    srv = B.make_server(0, rate_mbps=8.0)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    url = f"http://127.0.0.1:{port}/stream"
    try:
        assert B.url_reachable(url) is None
        assert B.url_reachable(f"http://127.0.0.1:{port + 1 if port < 65000 else port - 1}/x", timeout=1) is not None
        log = B.BenchLog(str(tmp_path / "dl.jsonl"))
        stop = threading.Event()
        dl = B.Downloader(url, log, stop, chunk_mb=1, out_path=str(tmp_path / "dl.bin"), report_s=0.2)
        probe = B.RttProbe([("dl", "127.0.0.1", port), ("ext", "127.0.0.1", 1)], log, stop, every_s=0.1, timeout_s=0.5)
        dl.start(); probe.start()
        time.sleep(1.6)
        stop.set()
        dl.join(5); probe.join(5)
        log.close()
    finally:
        srv.shutdown()
        srv.server_close()
    recs, _ = B.read_new(log.path, 0)
    d = [r for r in recs if r["k"] == "dl"]
    assert len(d) >= 3 and dl.errors == 0 and dl.pieces >= 2 and not [r for r in recs if r["k"] == "err"]
    mbps = sum(r["b"] for r in d) / 2 ** 20 / sum(r["s"] for r in d)
    assert 4.0 < mbps < 9.5, mbps                          # the source's rate cap holds
    ok = [r for r in recs if r["k"] == "rtt" and r["to"] == "dl"]
    assert len(ok) >= 5 and all(r["ms"] is not None and r["ms"] < 500 and 0 <= r["late"] < 500 for r in ok)
    refused = [r for r in recs if r["k"] == "rtt" and r["to"] == "ext"]
    assert refused and all(r["ms"] is None for r in refused)       # nothing listens on port 1
    assert not os.path.exists(str(tmp_path / "dl.bin"))            # the temp file is removed on exit


def test_parse_hostport():
    assert B.parse_hostport("www.baidu.com:443") == ("www.baidu.com", 443)
    assert B.parse_hostport("example.org") == ("example.org", 443)
    assert B.parse_hostport("http://10.0.2.2:8000/x") == ("10.0.2.2", 8000)


# ------------------------------------------------------------------ catalogue / trace
def test_bench_apps_are_in_the_catalogue_and_need_no_account():
    from cf.linux.onboard import login_spec
    from cf.linux.work import default_spec
    for id_, kind, cat in (("bench_numeric", "numeric", "compute"), ("bench_compress", "compress", "compute"),
                           ("bench_download", "download", "download")):
        a = A.CATALOG[id_]
        assert a.kind == "bench" and a.category == cat and f"--kind {kind}" in a.cmd and f"--app {id_}" in a.cmd
        import re
        assert re.search(a.match, f"python3 -m cf.linux.bench_app --app {id_} --kind {kind}")
        assert not re.search(a.match, f"python3 -m cf.linux.bench_app --app {id_}x --kind {kind}")
        assert re.search(a.wm_class, f"cf-{id_}")
        assert default_spec(a) == {"kind": "none", "oracle": "statefile"}
        assert login_spec(a)["method"] == "none"
    assert "--workers 2" in A.CATALOG["bench_compress"].cmd


def test_pattern_trace_repeats_the_given_order():
    apps = ["a", "b", "c", "d"]
    assert make_trace(apps, {"kind": "pattern", "pattern": ["a", "c", "b", "c"], "T": 9}) == list("acbcacbca")
    # unavailable apps drop out; what then stands twice in a row (also across the wrap) is merged
    assert make_trace(["a", "b", "c"], {"kind": "pattern", "pattern": ["a", "x", "a", "b", "c", "a"], "T": 6}) == list("abcabc")
    with pytest.raises(ValueError):
        make_trace(apps, {"kind": "pattern", "pattern": ["a", "zz"], "T": 4})


def test_classes_config_resolves_and_its_pattern_only_names_its_apps():
    cfg = json.load(open(os.path.join(REPO, "configs", "ubuntu_arm64_classes.json")))
    apps = set(cfg["apps"])
    cl = cfg["classes"]
    assert set(cl["interactive"]) | set(cl["compute"]) | set(cl["download"]) == apps
    assert set(cfg["trace"]["pattern"]) == apps and cfg["trace"]["kind"] == "pattern"
    pat = cfg["trace"]["pattern"]
    assert all(pat[i] != pat[i - 1] for i in range(len(pat)))       # incl. the wrap-around
    # every bench app is in the foreground once per round, every interactive app at least twice
    assert all(pat.count(a) == 1 for a in cl["compute"] + cl["download"])
    assert all(pat.count(a) >= 2 for a in cl["interactive"])
    assert cfg["trace"]["T"] % len(pat) == 0 and cfg["smooth_probe"]["n"] > 0 and cfg["work"] is True
    assert all(A.CATALOG[a].kind == "bench" for a in cl["compute"] + cl["download"])


# ------------------------------------------------------------------ runner + analyzer on the fake desktop
INTER = ["fake_office", "fake_chat", "fake_browser"]
APPS = INTER + list(BENCH_OVERRIDES)
PATTERN = ["fake_office", "fake_num", "fake_chat", "fake_dlb", "fake_browser", "fake_office", "fake_chat", "fake_browser"]


def cfg_for(tmp_path, policy, name, **kw):
    cfg = {"apps": APPS, "app_overrides": {**{a: OVERRIDES[a] for a in INTER}, **BENCH_OVERRIDES},
           "policy": policy, "eta": 0.0, "trace": {"kind": "pattern", "pattern": PATTERN, "T": 16, "seed": 1},
           "dwell_s": 0.25, "settle_s": 0, "warmup_dwell_s": 0, "work": True, "work_s": 0.01,
           "classes": {"interactive": INTER, "compute": ["fake_num"], "download": ["fake_dlb"]},
           "smooth_probe": {"n": 4, "every_s": 0.0, "timeout_s": 1.0},
           "throttle": {"cpu_cores": 0.25, "mem_frac": 0.1, "mem_min_mb": 32, "squeeze_rounds": 1, "thrash_high_per_s": 0},
           "out_dir": str(tmp_path), "name": name, "min_ram_mb": 1024}
    cfg.update(kw)
    return cfg


def run_fake(tmp_path, policy, name, **kw):
    cfg = cfg_for(tmp_path, policy, name, **kw)
    dev = FakeLinuxDesktop(APPS, seed=2)
    dev.ping_dead.add("fake_chat")
    dev.work_driver = FakeWorkDriver(dev, cfg)
    path = ThrottleExperiment(cfg, dev, log=lambda *a: None).run()
    return path, dev


@pytest.fixture(scope="module")
def two_runs(tmp_path_factory):
    d = tmp_path_factory.mktemp("classes")
    none, _ = run_fake(d, "none", "none_classes_s1")
    sq, _ = run_fake(d, "static", "squeeze_classes_s1")
    return d, load(none), load(sq)


def test_runner_records_bench_jobs_and_the_smooth_probe(two_runs):
    _, none, sq = two_runs
    assert none["meta"]["kinds"]["fake_num"] == "bench" and none["meta"]["config"]["classes"]["compute"] == ["fake_num"]
    assert [s["req"] for s in sq["steps"]] == PATTERN * 2
    for s in sq["steps"]:
        assert set(s["bench"]) == {"fake_num", "fake_dlb"} and s["t_dwell"] > 1e9
        jobs = [r for r in s["bench"]["fake_num"] if r["k"] == "job"]
        assert jobs and all("t" not in r and r["dt"] <= s["bg"]["fake_num"]["wall_s"] + 0.2 for r in jobs)
        sm = s["launch"]["smooth"]
        if s["req"] == "fake_chat":
            assert sm == {"supported": False, "pings_ms": []}
        else:
            assert sm["supported"] and len(sm["pings_ms"]) == 4
    # the jobs of the warmup are not attributed to step 0: nothing starts before the first switch began
    first = sq["steps"][0]
    assert min(r["dt"] for r in first["bench"]["fake_num"]) > -(first["step_s"] + 1.0)
    # the oracle's read-back time rides along with every state check
    assert any((s["launch"].get("work_check") or {}).get("after", {}).get("read_ms") == 180 for s in sq["steps"])


def test_intervals_follow_the_app_between_foreground_and_background(two_runs):
    _, _, sq = two_runs
    ivs = C.intervals(sq, "fake_num")
    assert [iv["pos"] for iv in ivs] == ["bg", "fg", "bg", "fg", "bg"]
    assert [len(iv["tiers"]) for iv in ivs] == [1, 1, 7, 1, 6]
    assert all(t in ("bg", "shrunk") for iv in ivs if iv["pos"] == "bg" for t in iv["tiers"])
    assert all(iv["tiers"] == ["fg"] for iv in ivs if iv["pos"] == "fg")
    assert all(a["t1"] <= b["t0"] for a, b in zip(ivs, ivs[1:]))
    ev = C.bench_events(sq, "fake_num")
    assert ev == sorted(ev, key=lambda e: e["T"]) and len(ev) > 50


def test_class_tables_compare_foreground_background_and_the_baseline(two_runs):
    _, none, sq = two_runs
    tb = C.tables([none, sq])
    comp = {r["run"]: r for r in tb["compute"]}
    n, s = comp["none_classes_s1"], comp["squeeze_classes_s1"]
    # unlimited: a job takes the same time wherever the app is; limited to a quarter core: 4x in the background
    assert n["fg_job_p50_ms"] == pytest.approx(4.0, rel=0.05) and n["slowdown_x"] == pytest.approx(1.0, abs=0.05)
    assert s["fg_job_p50_ms"] == pytest.approx(4.0, rel=0.05) and s["bg_job_p50_ms"] == pytest.approx(16.0, rel=0.05)
    assert s["slowdown_x"] == pytest.approx(4.0, rel=0.05) and s["bg_vs_none_x"] == pytest.approx(4.0, rel=0.05)
    assert n["bg_vs_none_x"] == pytest.approx(1.0)
    assert 15 < s["bg_throughput_pct"] < 35 and 80 < n["bg_throughput_pct"] < 120
    assert s["bg_s_per_100_jobs"] == pytest.approx(1.6, rel=0.25) and s["bad_jobs"] == 0
    assert s["bg_cpu_pct"] == pytest.approx(25, abs=2) and s["fg_cpu_pct"] == pytest.approx(100, abs=2)
    assert s["fg_jobs"] > 20 and s["bg_jobs"] > 50 and s["bg_s"] > 5 * s["fg_s"] and "×" in s["bg_tiers"]

    dl = {r["run"]: r for r in tb["download"]}
    n, s = dl["none_classes_s1"], dl["squeeze_classes_s1"]
    assert n["fg_mbps"] == pytest.approx(40, rel=0.05) and n["bg_speed_pct"] == pytest.approx(100, abs=5)
    assert s["bg_mbps"] == pytest.approx(10, rel=0.05) and s["bg_speed_pct"] == pytest.approx(25, abs=3)
    assert s["bg_vs_none_pct"] == pytest.approx(25, abs=3)
    # the network round trip is the same everywhere; what a limited app adds is the wait for its CPU share
    assert s["fg_rtt_p50_ms"] == s["bg_rtt_p50_ms"] == n["bg_rtt_p50_ms"] == 0.4
    assert s["fg_wake_p50_ms"] == 0 and s["bg_wake_p50_ms"] == 7.5 and n["bg_wake_p95_ms"] == 0
    assert s["fg_ext_rtt_p50_ms"] is None and s["rtt_lost_pct"] == 0 and s["errors"] == 0

    inter = {(r["run"], r["app"]): r for r in tb["interactive"]}
    assert {a for _, a in inter} == set(INTER) | {"ALL"}
    n, s = inter[("none_classes_s1", "ALL")], inter[("squeeze_classes_s1", "ALL")]
    # step 0 (nothing limited yet) is left out: 12 interactive steps -> 11 calls, all of them from a limited background
    assert n["n_calls"] == s["n_calls"] == 11 and n["n_limited"] == 0 and s["n_limited"] == 11 and s["n_cold"] == 0
    assert s["resp_p50_ms"] > n["resp_p50_ms"] and s["resp_p50_x"] > 1.0 and n["resp_p50_x"] == 1.0
    assert s["resp_p50_ms"] <= s["resp_p95_ms"] <= s["resp_max_ms"]
    # the probe: only the apps that answer pings; the first ping after a squeezed background is the slow one
    assert inter[("squeeze_classes_s1", "fake_chat")]["probe_n"] == 0
    assert inter[("squeeze_classes_s1", "fake_chat")]["stall_pct"] is None
    off = inter[("squeeze_classes_s1", "fake_office")]
    assert off["probe_n"] == 3 * 4 and off["probe_max_ms"] > 100 > off["probe_p50_ms"] and off["stall_pct"] == 25.0
    assert inter[("none_classes_s1", "fake_office")]["stall_pct"] == 0 and off["echo_p50_ms"] == 180
    assert s["work_checks"] == 11 and s["work_fail"] == 0


def test_classes_default_to_kind_and_category_without_a_config_entry(two_runs):
    _, none, _ = two_runs
    run = json.loads(json.dumps({k: none[k] for k in ("meta", "steps", "path")}))
    run["meta"]["config"].pop("classes")
    assert C.classes_of(run) == {"interactive": INTER, "compute": ["fake_num"], "download": ["fake_dlb"]}


def test_cli_writes_csv_report_workbook_and_figures(two_runs, capsys):
    d, none, sq = two_runs
    out = d / "out"
    assert C.main([none["path"], sq["path"], "--out", str(out)]) == 0
    text = capsys.readouterr().out
    assert "一、呼叫与切换的响应" in text and "二、计算型应用" in text and "三、下载型应用" in text
    rows = list(csv.DictReader(open(out / "classes_compute.csv")))
    assert [r["run"] for r in rows] == ["none_classes_s1", "squeeze_classes_s1"] and float(rows[1]["slowdown_x"]) > 3
    assert len(list(csv.DictReader(open(out / "classes_interactive.csv")))) == 2 * (len(INTER) + 1)
    assert len(list(csv.DictReader(open(out / "classes_download.csv")))) == 2
    md = (out / "classes_report.md").read_text()
    assert "squeeze_classes_s1" in md and "不限制 (基准)" in md and "全部交互应用" in md
    pytest.importorskip("openpyxl")
    from openpyxl import load_workbook
    wb = load_workbook(out / "classes_tables.xlsx")
    assert wb.sheetnames == ["呼叫与切换", "计算型", "下载型", "说明"]
    assert wb["计算型"].max_row == 3 and wb["呼叫与切换"]["A1"].value == "格"
    pytest.importorskip("matplotlib")
    for f in ("classes.png", "none_classes_s1_bench.png", "squeeze_classes_s1_bench.png"):
        assert (out / "plots" / f).stat().st_size > 10000


def test_resume_does_not_attribute_old_jobs_to_the_next_step(tmp_path):
    cfg = cfg_for(tmp_path, "static", "resume_classes")
    dev = FakeLinuxDesktop(APPS, seed=2)
    dev.work_driver = FakeWorkDriver(dev, cfg)
    dev.fail_at_launch = len(APPS) + 6
    with pytest.raises(OSError):
        ThrottleExperiment(cfg, dev, log=lambda *a: None).run()
    time.sleep(0.5)                                   # the bench apps keep running while the runner is away
    dev.fail_at_launch = None
    path = ThrottleExperiment(cfg, dev, log=lambda *a: None, resume=True).run()
    run = load(path)
    assert len(run["steps"]) == 16 and run["resumes"] == 1
    for s in run["steps"]:
        assert all(r["dt"] > -(s["step_s"] + 0.2) for r in s["bench"]["fake_num"])


# ------------------------------------------------------------------ the real window (Xvfb)
def _xvfb_available():
    import importlib.util
    import shutil
    return bool(shutil.which("Xvfb")) and importlib.util.find_spec("Xlib") is not None


@pytest.mark.skipif(not _xvfb_available(), reason="needs Xvfb + python3-xlib")
def test_bench_app_window_runs_jobs_and_writes_log_and_state(tmp_path):
    disp = ":%d" % (140 + os.getpid() % 50)
    xvfb = subprocess.Popen(["Xvfb", disp, "-screen", "0", "800x600x24"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    env = dict(os.environ, DISPLAY=disp, PYTHONPATH=REPO)
    log, state = str(tmp_path / "b.jsonl"), str(tmp_path / "b.json")
    proc = None
    try:
        time.sleep(1.0)
        proc = subprocess.Popen([sys.executable, "-m", "cf.linux.bench_app", "--app", "t_bench", "--kind", "numeric",
                                 "--scale", "0.02", "--log", log, "--state-file", state], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        deadline = time.time() + 15
        jobs = []
        while time.time() < deadline and (len(jobs) < 5 or not os.path.exists(state)):
            time.sleep(0.2)
            assert proc.poll() is None, proc.stderr.read().decode()
            jobs = [r for r in B.read_new(log, 0)[0] if r["k"] == "job"]
        assert len(jobs) >= 5 and all(r["ok"] for r in jobs)
        st = json.load(open(state))
        assert st["pid"] == proc.pid and st["kind"] == "numeric" and st["counter"] >= 1
        out = subprocess.run(["xprop", "-display", disp, "-root", "-notype", "_NET_CLIENT_LIST"], capture_output=True, text=True)
        tree = subprocess.run(["xwininfo", "-display", disp, "-root", "-tree"], capture_output=True, text=True).stdout \
            if out.returncode == 0 else ""
        if tree:
            assert "cf-t_bench" in tree
        kids = subprocess.run(["pgrep", "-P", str(proc.pid)], capture_output=True, text=True).stdout.split()
        assert len(kids) == 1                       # the job runs in a worker process, not in the window's thread
        proc.terminate()
        assert proc.wait(10) == 0                   # SIGTERM leaves through the normal cleanup
        time.sleep(0.3)
        assert not os.path.exists(f"/proc/{kids[0]}") or open(f"/proc/{kids[0]}/stat").read().split()[2] == "Z"
        assert json.load(open(state))["counter"] >= len(jobs)
    finally:
        if proc and proc.poll() is None:
            proc.kill()
        xvfb.terminate()
