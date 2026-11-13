"""The browser download app (cf.linux.browser_dl): a second Firefox that keeps one download running,
and the pieces that let it live next to the `firefox` of the interactive class."""
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request

import pytest

from cf.linux import apps as A
from cf.linux import browser_dl as BD
from cf.linux import device as D
from cf.linux import netstat as N
from cf.linux import onboard as OB
from cf.linux import procs as PR
from cf.linux.cgroup import CgroupTree

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIG = 300 * 2 ** 20


# ------------------------------------------------------------------ browser lookup
def _exe(path, text="#!/bin/sh\nexit 0\n"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, 0o755)
    return str(path)


def test_the_snap_firefox_is_not_taken_for_a_usable_one(tmp_path, monkeypatch):
    wrapper = _exe(tmp_path / "bin" / "firefox", "#!/bin/sh\n# Ubuntu's transitional package\nexec /snap/bin/firefox \"$@\"\n")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.delenv("CF_BROWSER", raising=False)
    assert BD.is_snap(wrapper)
    path, why = BD.find_browser()
    assert path == "" and "snap" in why and "packages.mozilla.org" in why
    assert BD.missing() == why

    esr = _exe(tmp_path / "bin" / "firefox-esr")            # a real build next to it is used instead
    assert BD.find_browser() == (esr, "")
    other = _exe(tmp_path / "opt" / "fx" / "firefox")
    monkeypatch.setenv("CF_BROWSER", other)                 # an explicit one wins
    assert BD.find_browser() == (other, "")

    monkeypatch.setenv("PATH", str(tmp_path / "nothing"))
    monkeypatch.delenv("CF_BROWSER")
    assert "no Firefox found" in BD.find_browser()[1]


# ------------------------------------------------------------------ source
def test_first_source_that_serves_a_large_file_is_used():
    answers = {"https://a/x.iso": (None, "HTTP 404"), "https://b/x.iso": (5 * 2 ** 20, "https://b/x.iso"),
               "https://c/x.iso": (BIG, "http://cdn.c/x.iso"), "http://d/x.iso": (BIG, "http://d/x.iso"),
               "https://e/x.iso": (BIG, "https://e/x.iso")}
    url, notes = BD.pick_source(list(answers), https_only=True, probe=lambda u: answers[u])
    assert url == "https://e/x.iso"
    assert len(notes) == 4 and "404" in notes[0] and "only 5 MB" in notes[1] and "not https" in notes[2]
    # without a rate to hold, plain http and redirects to it are fine
    assert BD.pick_source(list(answers), probe=lambda u: answers[u])[0] == "https://c/x.iso"
    assert BD.pick_source(["https://a/x.iso"], probe=lambda u: answers[u])[0] == ""


def test_own_urls_come_before_the_mirrors(monkeypatch):
    monkeypatch.setenv("CF_BROWSER_DL_URL", "https://env/1.iso https://env/2.iso")
    urls = BD.source_urls(["https://cli/0.iso"])
    assert urls[:3] == ["https://cli/0.iso", "https://env/1.iso", "https://env/2.iso"] and urls[3:] == list(BD.SOURCES)
    assert all(u.startswith("https://") for u in BD.SOURCES)


def test_probe_reads_the_size_from_a_head_request():
    class H(http.server.BaseHTTPRequestHandler):
        def do_HEAD(self):                      # noqa: N802
            self.send_response(200 if self.path == "/big" else 404)
            self.send_header("Content-Length", str(BIG))
            self.end_headers()

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        assert BD.probe_source(base + "/big") == (BIG, base + "/big")
        size, why = BD.probe_source(base + "/gone")
        assert size is None and "404" in why
    finally:
        srv.shutdown()
    assert BD.probe_source("http://127.0.0.1:1/x", timeout=2)[0] is None


# ------------------------------------------------------------------ pacing relay
def _source_server(payload: bytes):
    """A TCP server that answers any first line with the payload (stands in for the TLS server
    behind the tunnel: the relay never looks inside)."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)

    def serve():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            try:
                c.recv(100)
                c.sendall(payload)
            finally:
                c.close()
    threading.Thread(target=serve, daemon=True).start()
    return srv


def _through(relay_port: int, target: str, hello: bytes = b"hello") -> tuple[bytes, bytes]:
    c = socket.create_connection(("127.0.0.1", relay_port), timeout=10)
    c.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
    head = b""
    while b"\r\n\r\n" not in head:
        d = c.recv(1)
        if not d:
            break
        head += d
    body = b""
    if b" 200 " in head:
        c.sendall(hello)
        while True:
            d = c.recv(65536)
            if not d:
                break
            body += d
    c.close()
    return head, body


def test_relay_passes_the_bytes_on_at_the_rate_asked_for():
    payload = os.urandom(600_000)
    src = _source_server(payload)
    relay = BD.Relay(400_000)                     # bytes per second
    relay.start()
    try:
        t0 = time.monotonic()
        head, body = _through(relay.port, f"127.0.0.1:{src.getsockname()[1]}")
        took = time.monotonic() - t0
        assert b" 200 " in head and body == payload        # nothing lost, nothing reordered
        assert 1.0 <= took <= 3.0, took                     # 600 kB at 400 kB/s (a 0.2 s burst is allowed)
        assert relay.bytes_down == len(payload)
        # two tunnels share the one rate
        out = []
        ts = [threading.Thread(target=lambda: out.append(_through(relay.port, f"127.0.0.1:{src.getsockname()[1]}")[1]))
              for _ in range(2)]
        t0 = time.monotonic()
        [t.start() for t in ts]
        [t.join(20) for t in ts]
        assert out == [payload, payload] and time.monotonic() - t0 >= 2.4
    finally:
        relay.close()
        src.close()


def test_relay_refuses_what_is_not_a_tunnel_and_reports_a_dead_target():
    relay = BD.Relay(1e6, connect_timeout=2)
    relay.start()
    try:
        c = socket.create_connection(("127.0.0.1", relay.port), timeout=5)
        c.sendall(b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n")
        assert b" 405 " in c.recv(200)
        c.close()
        head, body = _through(relay.port, "127.0.0.1:1")
        assert b" 502 " in head and body == b""
    finally:
        relay.close()


def test_pace_hands_out_the_rate_not_more():
    p = BD.Pace(100_000, burst_s=0.1)
    t0 = time.monotonic()
    for _ in range(10):
        p.wait(5_000)                              # 50 kB at 100 kB/s
    assert 0.35 <= time.monotonic() - t0 <= 0.8


# ------------------------------------------------------------------ control page
def test_page_takes_a_url_once():
    ctl = BD.Control()
    try:
        page = urllib.request.urlopen(ctl.url, timeout=5).read().decode()
        assert "iframe" in page and "/next" in page
        nxt = lambda: json.load(urllib.request.urlopen(ctl.url + "next", timeout=5))      # noqa: E731
        assert ctl.t_poll is None
        assert nxt()["url"] == "" and ctl.t_poll is not None
        got = []
        t = threading.Thread(target=lambda: got.append(ctl.hand("https://x/file.iso", timeout=5)))
        t.start()
        time.sleep(0.2)
        ctl.status = "downloading"
        assert nxt() == {"url": "https://x/file.iso", "status": "downloading"}
        t.join(5)
        assert got == [True] and nxt()["url"] == ""           # handed over exactly once
        assert ctl.hand("https://x/never.iso", timeout=0.3) is False      # nobody asked
        assert nxt()["url"] == ""                              # ... and it is not left lying around
    finally:
        ctl.close()


# ------------------------------------------------------------------ profile
def test_profile_downloads_without_asking_and_forgets_the_last_session(tmp_path):
    prof, dl = tmp_path / "profile", tmp_path / "downloads"
    os.makedirs(prof / "sessionstore-backups")
    for name in ("downloads.json", "sessionstore.jsonlz4", "places.sqlite"):
        (prof / name).write_text("x")
    path = BD.write_profile(str(prof), str(dl), proxy_port=4567)
    text = open(path).read()
    assert f'user_pref("browser.download.dir", "{dl}");' in text
    assert 'user_pref("browser.download.useDownloadDir", true);' in text
    assert 'user_pref("network.http.http3.enable", false);' in text           # ss -t does not see QUIC
    assert 'user_pref("network.proxy.type", 1);' in text and 'user_pref("network.proxy.ssl_port", 4567);' in text
    assert dl.is_dir() and not (prof / "downloads.json").exists() and not (prof / "sessionstore-backups").exists()
    assert (prof / "places.sqlite").exists()                                   # the rest of the profile is kept
    direct = BD.prefs(str(dl))
    assert direct["network.proxy.type"] == 0 and "network.proxy.ssl" not in direct


# ------------------------------------------------------------------ keeper
class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _keeper(tmp_path, urls=("https://a/f.iso", "https://b/f.iso"), opened=None, ok=True, **kw):
    dl = tmp_path / "downloads"
    dl.mkdir(exist_ok=True)
    clock, log = Clock(), []
    opened = [] if opened is None else opened

    def open_url(u):
        opened.append(u)
        return ok
    k = BD.Keeper(str(dl), list(urls), open_url, log=log.append, clock=clock,
                  pick=lambda us, https_only=False: (us[0], []), **kw)
    return k, dl, clock, log, opened


def test_keeper_starts_a_download_and_the_next_one_when_it_is_complete(tmp_path):
    k, dl, clock, log, opened = _keeper(tmp_path)
    (dl / "old.iso").write_bytes(b"x" * 10)                 # left by an earlier run
    assert k.tick() == "started" and opened == ["https://a/f.iso"] and not (dl / "old.iso").exists()
    clock.t += 2
    assert k.tick() == "waiting" and len(opened) == 1       # asked for, no file yet: do not ask again
    part, final = dl / "f.Ab12.iso.part", dl / "f.iso"
    final.write_bytes(b"")                                  # what Firefox creates: empty placeholder + .part
    part.write_bytes(b"x" * 100)
    clock.t += 2
    assert k.tick() == "downloading"
    assert final.exists()                                   # the placeholder of a running download is left alone
    part.write_bytes(b"x" * 300)
    clock.t += 2
    assert k.tick() == "downloading"
    clock.t += 2
    assert k.tick() == "waiting" and len(opened) == 1       # no growth in one look is not the end
    os.remove(part)                                         # complete: the .part becomes the file
    final.write_bytes(b"x" * 500)
    clock.t += 2
    assert k.tick() == "started" and len(opened) == 2
    assert os.listdir(dl) == [] and k.completed == 1 and k.done_bytes == 500 and k.started == 2
    assert any("complete" in m for m in log)


def test_keeper_drops_a_stalled_download_and_tries_the_other_source(tmp_path):
    k, dl, clock, log, opened = _keeper(tmp_path, stall_s=300)
    k.tick()
    (dl / "f.iso").write_bytes(b"")
    (dl / "f.x.iso.part").write_bytes(b"x" * 100)
    clock.t += 2
    assert k.tick() == "downloading"
    clock.t += 299
    assert k.tick() == "waiting"
    clock.t += 2
    assert k.tick() == "started" and opened == ["https://a/f.iso", "https://b/f.iso"]
    assert os.listdir(dl) == [] and k.stalled == 1 and k.bad == ["https://a/f.iso"]


def test_keeper_moves_on_when_a_source_never_produces_a_file(tmp_path):
    k, dl, clock, log, opened = _keeper(tmp_path, grace_s=45)
    k.tick()
    clock.t += 44
    assert k.tick() == "waiting"
    clock.t += 2
    assert k.tick() == "started" and opened == ["https://a/f.iso", "https://b/f.iso"]
    assert any("no file appeared" in m for m in log)
    clock.t += 46                                          # both failed once: start over with the first
    assert k.tick() == "started" and opened[-1] == "https://a/f.iso"


def test_keeper_waits_when_the_browser_does_not_take_the_url_or_the_disk_is_full(tmp_path, monkeypatch):
    k, dl, clock, log, opened = _keeper(tmp_path, ok=False)
    assert k.tick() == "idle" and len(opened) == 1 and k.started == 0
    clock.t += 10
    assert k.tick() == "waiting" and len(opened) == 1      # retries are grace_s apart
    clock.t += 40
    assert k.tick() == "idle" and len(opened) == 2

    k2, dl2, clock2, log2, opened2 = _keeper(tmp_path, min_free_gb=3)
    monkeypatch.setattr(k2, "free_gb", lambda: 1.5)
    assert k2.tick() == "idle" and opened2 == [] and any("1.5 GB free" in m for m in log2)
    monkeypatch.setattr(k2, "free_gb", lambda: 50.0)
    clock2.t += 46
    assert k2.tick() == "started" and len(opened2) == 1


# ------------------------------------------------------------------ catalogue, cgroups, onboarding
def test_catalogue_keeps_the_two_firefoxes_apart(tmp_path, monkeypatch):
    import re
    fx, dl = A.CATALOG["firefox"], A.CATALOG["browser_dl"]
    assert dl.category == "download" and dl.kind == "desktop" and "cf.linux.browser_dl --app browser_dl" in dl.cmd
    main = "/usr/lib/firefox/firefox --name cf-browser_dl --class cf-browser_dl --profile /home/u/.local/share/context_freeze/browser_dl/profile http://127.0.0.1:4000/"
    keeper = "python3 -m cf.linux.browser_dl --app browser_dl"
    child = "/usr/lib/firefox/firefox-bin -contentproc -isForBrowser -prefsHandle 0:26555 -parentBuildID 2026 tab"
    assert re.search(dl.match, main) and re.search(dl.match, keeper)
    assert not re.search(dl.match, "/usr/lib/firefox/firefox") and not re.search(dl.match, child)
    assert re.search(fx.not_under, main) and not re.search(fx.not_under, "/usr/lib/firefox/firefox --profile /home/u/x")
    # windows: every Firefox window has the instance name "Navigator"; only the class tells them apart
    cls = lambda rx, wc: any(re.search(rx, c, re.I) for c in wc)        # noqa: E731
    assert cls(fx.wm_class, ["Navigator", "firefox"]) and cls(fx.wm_class, ["Navigator", "firefox-esr"])
    assert not cls(fx.wm_class, ["Navigator", "cf-browser_dl"])
    assert cls(dl.wm_class, ["Navigator", "cf-browser_dl"]) and not cls(dl.wm_class, ["Navigator", "firefox"])
    assert BD.wm_class("browser_dl") == "cf-browser_dl"
    # installed = a usable Firefox is there
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv("CF_BROWSER", raising=False)
    assert not A.is_installed(dl)
    monkeypatch.setenv("CF_BROWSER", _exe(tmp_path / "fx" / "firefox"))
    assert A.is_installed(dl)
    assert OB.self_managed(dl) and not OB.self_managed(fx)
    assert OB.login_spec(dl)["method"] == "none"


class Tree(CgroupTree):
    """A real CgroupTree API over a fake membership table."""

    def __init__(self, members):
        super().__init__(root="/nonexistent")
        self.members = members                    # pid -> leaf

    def exists(self, name=""):
        return True

    def procs(self, name):
        return sorted(p for p, leaf in self.members.items() if leaf == name)

    def leaf_of(self, pid):
        return self.members.get(pid)


def test_a_second_instance_is_not_a_stray_of_the_first(monkeypatch):
    """`firefox`'s process regex matches every Firefox process.  Those of the instance browser_dl
    drives must not be adopted into /cf/firefox - neither while they sit in /cf/browser_dl nor
    when they have left it."""
    cmd = {
        10: "/usr/lib/firefox/firefox",                                             # the interactive one
        11: "/usr/lib/firefox/firefox-bin -contentproc -isForBrowser tab",
        20: "python3 -m cf.linux.browser_dl --app browser_dl",
        21: "/usr/lib/firefox/firefox --name cf-browser_dl --class cf-browser_dl --profile /p http://127.0.0.1:1/",
        22: "/usr/lib/firefox/firefox-bin -contentproc -isForBrowser tab",
        30: "/usr/lib/firefox/firefox-bin -contentproc started-by-hand",
    }
    parent = {11: 10, 21: 20, 22: 21}
    import re
    monkeypatch.setattr(PR, "find_pids", lambda rx, pids=None, uid=None: [p for p in cmd if re.search(rx, cmd[p])])

    def children_of(roots):
        want = set(roots)
        while True:
            more = {p for p, pp in parent.items() if pp in want} - want
            if not more:
                return sorted(want)
            want |= more
    monkeypatch.setattr(PR, "children_of", children_of)
    cat = A.with_overrides({})

    # everything where it belongs, one Firefox process started from a terminal inside /cf/host
    tree = Tree({10: "firefox", 11: "firefox", 20: "browser_dl", 21: "browser_dl", 22: "browser_dl", 30: "host"})
    dev = D.LinuxDesktop(cat=cat, tree=tree, notifications=False, mobile=None)
    assert dev.stray_pids("firefox") == [30]
    assert dev.stray_pids("browser_dl") == []
    with monkeypatch.context() as m:           # a process in another app's cgroup is never a stray, whatever it matches
        m.setattr(cat["firefox"], "not_under", "")
        assert dev.stray_pids("firefox") == [30]

    # the download browser's processes left their cgroup (session scope): still not firefox's
    tree.members.update({21: None, 22: None})
    assert dev.stray_pids("firefox") == [30]
    assert dev.stray_pids("browser_dl") == [21]            # adopt() takes its children along
    assert children_of(dev.stray_pids("browser_dl")) == [21, 22]

    # without the ancestry rule the content process 22 would have been claimed
    monkeypatch.setattr(cat["firefox"], "not_under", "")
    assert dev.stray_pids("firefox") == [21, 22, 30]


def test_onboarding_does_not_ask_for_the_self_driving_browser(monkeypatch):
    from cf.linux.runner import ThrottleExperiment
    logged = []

    class Log:
        def warn(self, m):
            logged.append(m)

    class Dev:
        cat = A.with_overrides({})
    exp = ThrottleExperiment.__new__(ThrottleExperiment)
    exp.apps = ["firefox", "browser_dl"]
    exp.kinds = {"firefox": "desktop", "browser_dl": "desktop"}
    exp.dev, exp.log = Dev(), Log()
    monkeypatch.setattr(OB, "load_state", lambda: {})
    exp._preflight_onboarding()
    assert len(logged) == 1 and "firefox" in logged[0] and "browser_dl" not in logged[0]


# ------------------------------------------------------------------ disk budget, config
def test_disk_budget_counts_rate_times_time_except_for_the_file_that_is_reused():
    cmds = {"baidunetdisk": "baidunetdisk", "xunlei": "thunder", "browser_dl": A.CATALOG["browser_dl"].cmd}
    need, line = N.disk_budget({"baidunetdisk": 0.5, "xunlei": 1.0, "browser_dl": 2.0}, 360, cmds)
    # 0.5 MB/s x 6 h = 10.5 GB, 1 MB/s x 6 h = 21.1 GB, the browser re-uses one file
    assert abs(need - (10.55 + 21.09 + BD.FILE_GB)) < 0.1
    assert "baidunetdisk writes about 10.5 GB" in line and "browser_dl re-uses one file" in line


def test_full_config_has_the_three_classes():
    cfg = json.load(open(os.path.join(REPO, "configs", "ubuntu_arm64_full.json")))
    compute = json.load(open(os.path.join(REPO, "configs", "ubuntu_arm64_compute.json")))
    classes = json.load(open(os.path.join(REPO, "configs", "ubuntu_arm64_classes.json")))
    cl = cfg["classes"]
    assert cl["interactive"] == ["wechat", "qq", "wps", "firefox"]
    assert cl["compute"] == compute["classes"]["compute"] and len(cl["compute"]) == 6
    assert cl["download"] == ["baidunetdisk", "xunlei", "browser_dl"]
    assert cfg["apps"] == cl["interactive"] + cl["compute"] + cl["download"]
    assert cfg["throttle"] == compute["throttle"] and cfg["net_probe"] == classes["net_probe"]
    pat = cfg["trace"]["pattern"]
    assert cfg["trace"]["kind"] == "pattern" and len(pat) == 17 and cfg["trace"]["T"] % 17 == 0
    for a in cl["interactive"]:                     # called twice a round, never twice in a row (also across rounds)
        idx = [i for i, x in enumerate(pat) if x == a]
        assert len(idx) == 2 and idx[1] - idx[0] >= 4 and idx[0] + 17 - idx[1] >= 4
    for a in cl["compute"] + cl["download"]:
        assert pat.count(a) == 1
    cat = A.with_overrides(cfg.get("app_overrides"))
    assert all(a in cat for a in cfg["apps"])


def test_run_script_knows_the_full_config():
    env = dict(os.environ, DRY="1", CONFIG="configs/ubuntu_arm64_full.json", ROUNDS="6", SEEDS="1 2 3", OUT="/tmp/cf_dry_full")
    out = subprocess.run(["bash", "scripts/ubuntu/run_classes.sh"], cwd=REPO, env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert "rounds=6 (T=102)" in out.stdout and out.stdout.count("run_linux.py configs/ubuntu_arm64_full.json") == 9
    shutil.rmtree("/tmp/cf_dry_full", ignore_errors=True)


# ------------------------------------------------------------------ the real browser
def _display_ok() -> bool:
    if not os.environ.get("DISPLAY") or not shutil.which("xprop"):
        return False
    return subprocess.run(["xprop", "-root", "_NET_SUPPORTING_WM_CHECK"], capture_output=True, timeout=10).returncode == 0


@pytest.mark.skipif(bool(BD.missing()) or not _display_ok(), reason="needs a Firefox (deb) and an X display")
def test_real_firefox_downloads_restarts_and_stops(tmp_path):
    """The real thing against a local source: the page starts the download in Firefox's own download
    manager, a complete file is removed and fetched again, SIGTERM stops the browser and cleans up."""
    size = BD.MIN_SOURCE_MB * 2 ** 20
    served = []

    class H(http.server.BaseHTTPRequestHandler):
        def _head(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.end_headers()

        def do_HEAD(self):                      # noqa: N802
            self._head()

        def do_GET(self):                       # noqa: N802
            self._head()
            served.append(time.monotonic())
            chunk = b"\0" * (1 << 20)
            try:
                for _ in range(size >> 20):
                    self.wfile.write(chunk)
            except OSError:
                pass

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/big.bin"
    base = tmp_path / "bdl"
    log = open(tmp_path / "log.txt", "w")
    env = dict(os.environ, PYTHONPATH=REPO)
    p = subprocess.Popen([sys.executable, "-m", "cf.linux.browser_dl", "--app", "cftest", "--url", url, "--rate-mbps", "0",
                          "--dir", str(base), "--every", "0.5"], cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 120
        while len(served) < 2 and time.monotonic() < deadline and p.poll() is None:
            time.sleep(0.5)                     # the second GET = the first file was completed, removed and asked for again
        text = open(tmp_path / "log.txt").read()
        assert len(served) >= 2, text
        assert "download 1 started (first)" in text and "download complete: 0.20 GB" in text and "(again)" in text
        from cf.linux import x11
        wins = x11.windows_for([], r"^cf-cftest$")
        assert wins and "浏览器下载" in wins[0]["name"]
        assert os.path.getsize(base / "profile" / "user.js") > 0
    finally:
        p.terminate()
        try:
            p.wait(30)
        finally:
            if p.poll() is None:
                p.kill()
            srv.shutdown()
            log.close()
    assert "stopped" in open(tmp_path / "log.txt").read()
    time.sleep(1.0)
    assert not PR.find_pids(r"--class cf-cftest\b")           # the browser went with it
    assert os.listdir(base / "downloads") == []                # nothing left on disk
