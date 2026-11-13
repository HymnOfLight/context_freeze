"""A browser that keeps downloading - the third real download app of the three-class experiment
(manual §6.9), next to 百度网盘 and 迅雷.

    python3 -m cf.linux.browser_dl --app browser_dl            # what the catalogue entry `browser_dl` runs
    python3 -m cf.linux.browser_dl --check                     # is a usable Firefox there, which source answers

It starts a **second Firefox** - its own profile, its own window class (`cf-<app>`), so it is a
different app from the `firefox` of the interactive class and lives in its own cgroup - and keeps
one download of a large file running in it through Firefox's own download manager.  The browser
shows a small page served by this process; the page asks every two seconds whether there is a
file to fetch and, when there is, navigates a hidden frame to it - exactly what a click on a
download link does:

* the file is saved to disk like any download (`<data home>/<app>/downloads`); when it is complete
  it is deleted and the download starts again, so a run of any length needs only one file's worth
  of disk (4.8 GB for the default Ubuntu image);
* the speed is held at `--rate-mbps` (default 2 MB/s; `CF_BROWSER_DL_RATE_MBPS`) so that a night
  does not pull a terabyte from a public mirror and the cells stay comparable.  Firefox has no
  speed limit of its own, so its HTTPS connections go through a small CONNECT relay in this
  process (same cgroup, same limits) that hands the bytes on at that rate - TLS, HTTP and the
  writing are still done by the browser.  `--rate-mbps 0`: no relay, the browser connects
  directly and downloads as fast as the network allows;
* sources: `--url` / `CF_BROWSER_DL_URL` (space separated), then a list of mirrors of the same
  Ubuntu image; the first one that answers with a large file is used.

Nothing here needs an account.  The snap build of Firefox cannot be used (it may not read a profile
outside its own directories and snapd moves its processes out of our cgroup): install the deb from
packages.mozilla.org (manual §6.9).
"""
from __future__ import annotations

import argparse
import glob
import http.server
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Optional, Sequence

ISO = "ubuntu-22.04.5-desktop-amd64.iso"
SOURCES = (
    f"https://mirrors.tuna.tsinghua.edu.cn/ubuntu-releases/22.04/{ISO}",
    f"https://mirrors.bfsu.edu.cn/ubuntu-releases/22.04/{ISO}",
    f"https://mirrors.nju.edu.cn/ubuntu-releases/22.04/{ISO}",
    f"https://mirrors.huaweicloud.com/ubuntu-releases/22.04/{ISO}",
    f"https://mirrors.zju.edu.cn/ubuntu-releases/22.04/{ISO}",
    f"https://releases.ubuntu.com/22.04/{ISO}",
)
MIN_SOURCE_MB = 200          # anything smaller is over before a background stretch ends
FILE_GB = 4.8                # disk the default file takes (only one copy exists at a time)
BROWSERS = ("firefox", "firefox-esr")
INSTALL = ("Firefox deb from packages.mozilla.org (not the snap): manual §6.9 '下载型三个应用怎么配置', "
           "or https://support.mozilla.org/kb/install-firefox-linux")


def data_home() -> str:
    return os.environ.get("CF_DATA_HOME") or os.path.expanduser("~/.local/share/context_freeze")


def wm_class(app: str) -> str:
    return f"cf-{app}"


def is_snap(path: str) -> bool:
    """/snap/bin/firefox, or Ubuntu's /usr/bin/firefox wrapper script that execs it."""
    real = os.path.realpath(path)
    if "/snap/" in real or os.path.basename(real) == "snap":
        return True
    try:
        with open(real, "rb") as f:
            head = f.read(2048)
    except OSError:
        return False
    return head.startswith(b"#!") and b"/snap/" in head


def find_browser() -> tuple[str, str]:
    """(path, "") of a Firefox that can be driven, or ("", why not)."""
    names = ([os.environ["CF_BROWSER"]] if os.environ.get("CF_BROWSER") else []) + list(BROWSERS)
    snap = ""
    for name in names:
        p = name if os.path.isabs(name) and os.access(name, os.X_OK) else shutil.which(name)
        if not p:
            continue
        if is_snap(p):
            snap = p
            continue
        return p, ""
    if snap:
        return "", f"{snap} is the snap build, which cannot be used here - {INSTALL}"
    return "", f"no Firefox found - {INSTALL}"


def missing() -> str:
    """"" when the app can run."""
    return find_browser()[1]


# ------------------------------------------------------------------ source
def probe_source(url: str, timeout: float = 10.0) -> tuple[Optional[int], str]:
    """(size in bytes, final url) of a downloadable file, (None, why) otherwise."""
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0 (cf.linux.browser_dl)"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            size = int(r.headers.get("Content-Length") or 0)
            return (size, r.geturl()) if r.status == 200 else (None, f"HTTP {r.status}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        return None, f"{type(e).__name__}: {e}"[:120]


def pick_source(urls: Sequence[str], https_only: bool = False, probe=probe_source) -> tuple[str, list[str]]:
    """The first url that serves a large file -> (url, notes about the ones passed over)."""
    notes = []
    for u in urls:
        size, final = probe(u)
        if size is None:
            notes.append(f"{u}: {final}")
        elif size < MIN_SOURCE_MB * 2 ** 20:
            notes.append(f"{u}: only {size / 2 ** 20:.0f} MB (need >= {MIN_SOURCE_MB})")
        elif https_only and not (u.startswith("https://") and final.startswith("https://")):
            notes.append(f"{u}: not https all the way ({final}) - only https can be held at a rate")
        else:
            return u, notes
    return "", notes


def source_urls(cli: Optional[Sequence[str]] = None) -> list[str]:
    return list(cli or []) + os.environ.get("CF_BROWSER_DL_URL", "").split() + list(SOURCES)


# ------------------------------------------------------------------ pacing relay
class Pace:
    """Hands out bytes at `rate` per second (a short burst allowed), shared by all connections."""

    def __init__(self, rate: float, burst_s: float = 0.2):
        self.rate, self.burst_s = float(rate), burst_s
        self._t = time.monotonic()
        self._lock = threading.Lock()

    def wait(self, n: int) -> float:
        with self._lock:
            now = time.monotonic()
            self._t = max(self._t, now - self.burst_s) + n / self.rate
            delay = self._t - now
        if delay > 0:
            time.sleep(delay)
        return max(0.0, delay)


class Relay(threading.Thread):
    """HTTP CONNECT proxy on the loopback: bytes towards the browser are paced, nothing is decoded."""

    def __init__(self, rate_bytes: float, connect_timeout: float = 20.0):
        super().__init__(daemon=True)
        self.pace = Pace(rate_bytes)
        self.chunk = int(max(4096, min(65536, rate_bytes / 16)))
        self.connect_timeout = connect_timeout
        self.bytes_down = 0
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(64)
        self.port = self.srv.getsockname()[1]

    def run(self) -> None:
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def close(self) -> None:
        try:
            self.srv.close()
        except OSError:
            pass

    def _serve(self, c: socket.socket) -> None:
        up = None
        try:
            c.settimeout(30)
            head = b""
            while b"\r\n\r\n" not in head and len(head) < 16384:
                d = c.recv(4096)
                if not d:
                    return
                head += d
            words = head.split(b"\r\n", 1)[0].decode("latin-1").split()
            if len(words) < 2 or words[0].upper() != "CONNECT":
                c.sendall(b"HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\n\r\n")
                return
            host, _, port = words[1].rpartition(":")
            try:
                up = socket.create_connection((host.strip("[]"), int(port)), timeout=self.connect_timeout)
            except (OSError, ValueError):
                c.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
                return
            c.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            c.settimeout(None)
            up.settimeout(None)
            t = threading.Thread(target=self._pump, args=(c, up, False), daemon=True)
            t.start()
            self._pump(up, c, True)
            t.join(5)
        except OSError:
            pass
        finally:
            for s in (c, up):
                if s is not None:
                    try:
                        s.close()
                    except OSError:
                        pass

    def _pump(self, src: socket.socket, dst: socket.socket, paced: bool) -> None:
        try:
            while True:
                d = src.recv(self.chunk)
                if not d:
                    break
                if paced:
                    self.pace.wait(len(d))
                    self.bytes_down += len(d)
                dst.sendall(d)
        except OSError:
            pass
        finally:
            for s, how in ((dst, socket.SHUT_WR), (src, socket.SHUT_RD)):
                try:
                    s.shutdown(how)
                except OSError:
                    pass


# ------------------------------------------------------------------ control page
PAGE = """<!doctype html>
<meta charset="utf-8">
<title>浏览器下载 (context_freeze)</title>
<body style="font: 16px sans-serif; margin: 2em">
<h3>浏览器下载负载 / browser download load</h3>
<p>这个窗口由实验脚本管理: 它在反复下载同一个大文件。不要关闭, 也不用操作。</p>
<p id="s">…</p>
<iframe id="f" style="display: none"></iframe>
<script>
async function poll() {
  try {
    const j = await (await fetch('/next', {cache: 'no-store'})).json();
    if (j.url) document.getElementById('f').src = j.url;
    document.getElementById('s').textContent = j.status || '';
  } catch (e) {}
  setTimeout(poll, 2000);
}
poll();
</script>
"""


class Control:
    """The page the browser shows, and the one-slot mailbox it polls: `hand(url)` puts a url in,
    the page's next poll takes it out and starts the download."""

    def __init__(self):
        self.status = ""
        self.t_poll: Optional[float] = None      # last time the page asked
        self._url = ""
        self._taken = threading.Event()
        self._lock = threading.Lock()
        ctl = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):                      # noqa: N802
                if self.path.startswith("/next"):
                    body, ctype = json.dumps(ctl.take()).encode(), "application/json"
                elif self.path in ("/", "/index.html"):
                    body, ctype = PAGE.encode(), "text/html; charset=utf-8"
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def take(self) -> dict:
        with self._lock:
            self.t_poll = time.monotonic()
            url, self._url = self._url, ""
            if url:
                self._taken.set()
            return {"url": url, "status": self.status}

    def hand(self, url: str, timeout: float = 30.0) -> bool:
        """True when the page took the url within `timeout`."""
        with self._lock:
            self._url = url
            self._taken.clear()
        if self._taken.wait(timeout):
            return True
        with self._lock:
            self._url = ""
        return False

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()


# ------------------------------------------------------------------ profile
def prefs(dl_dir: str, proxy_port: int = 0) -> dict:
    p = {
        # save without asking, into our directory
        "browser.download.dir": dl_dir,
        "browser.download.folderList": 2,
        "browser.download.useDownloadDir": True,
        "browser.download.always_ask_before_handling_new_types": False,
        "browser.download.alwaysOpenPanel": False,
        "browser.download.manager.addToRecentDocs": False,
        "browser.download.start_downloads_in_tmp_dir": False,
        # a url given on the command line loads in the tab that is there (no tab per restart)
        "browser.link.open_newwindow.override.external": 1,
        # TCP only: the socket sampler (ss -t) does not see QUIC
        "network.http.http3.enable": False,
        # no first-run pages, questions or background traffic of its own
        "browser.shell.checkDefaultBrowser": False,
        "browser.aboutwelcome.enabled": False,
        "browser.startup.homepage": "about:blank",
        "browser.startup.page": 0,
        "browser.newtabpage.enabled": False,
        "browser.startup.homepage_override.mstone": "ignore",
        "startup.homepage_welcome_url": "",
        "startup.homepage_welcome_url.additional": "",
        "browser.sessionstore.resume_from_crash": False,
        "browser.tabs.warnOnClose": False,
        "browser.warnOnQuit": False,
        "datareporting.policy.dataSubmissionEnabled": False,
        "datareporting.healthreport.uploadEnabled": False,
        "toolkit.telemetry.enabled": False,
        "toolkit.telemetry.unified": False,
        "app.normandy.enabled": False,
        "app.shield.optoutstudies.enabled": False,
        "browser.safebrowsing.downloads.remote.enabled": False,
        "extensions.update.enabled": False,
        "browser.discovery.enabled": False,
        "network.captive-portal-service.enabled": False,
        "network.connectivity-service.enabled": False,
        # ... including the ~80 MB a new profile fetches in its first minutes (block lists, certificate
        # filters, codecs, remote settings), which would share the rate and show up as download speed
        "services.settings.server": "data:,#remote-settings-dummy/v1",
        "browser.safebrowsing.malware.enabled": False,
        "browser.safebrowsing.phishing.enabled": False,
        "browser.safebrowsing.blockedURIs.enabled": False,
        "browser.safebrowsing.provider.google4.updateURL": "",
        "browser.safebrowsing.provider.google.updateURL": "",
        "browser.safebrowsing.provider.mozilla.updateURL": "",
        "security.remote_settings.crlite_filters.enabled": False,
        "security.remote_settings.intermediates.enabled": False,
        "media.gmp-manager.updateEnabled": False,
        "media.gmp-provider.enabled": False,
        "extensions.systemAddon.update.enabled": False,
        "extensions.getAddons.cache.enabled": False,
        "extensions.blocklist.enabled": False,
        "browser.search.update": False,
        "browser.topsites.contile.enabled": False,
        "browser.newtabpage.activity-stream.feeds.topsites": False,
        "browser.newtabpage.activity-stream.feeds.section.topstories": False,
        "browser.ml.enable": False,
        "browser.translations.enable": False,
        "network.prefetch-next": False,
        "network.dns.disablePrefetch": True,
        "network.predictor.enabled": False,
        "dom.push.enabled": False,
        "network.proxy.type": 1 if proxy_port else 0,
    }
    if proxy_port:
        p.update({"network.proxy.ssl": "127.0.0.1", "network.proxy.ssl_port": proxy_port,
                  "network.proxy.no_proxies_on": "", "network.proxy.allow_hijacking_localhost": False})
    return p


def write_profile(profile: str, dl_dir: str, proxy_port: int = 0) -> str:
    """user.js with our prefs; what the last session left to resume (downloads, tabs) is dropped."""
    import json
    os.makedirs(profile, exist_ok=True)
    os.makedirs(dl_dir, exist_ok=True)
    for name in ("downloads.json", "sessionstore.jsonlz4", "sessionCheckpoints.json"):
        try:
            os.remove(os.path.join(profile, name))
        except OSError:
            pass
    shutil.rmtree(os.path.join(profile, "sessionstore-backups"), ignore_errors=True)
    path = os.path.join(profile, "user.js")
    with open(path, "w") as f:
        for k, v in prefs(dl_dir, proxy_port).items():
            f.write(f"user_pref({json.dumps(k)}, {json.dumps(v)});\n")
    return path


# ------------------------------------------------------------------ keeper
class Keeper:
    """Keeps one download running: watches the download directory (Firefox writes `<name>.part`
    while it downloads), deletes what is complete and starts the next one.

    `open_url(url) -> bool` starts a download; everything else is files, so it is testable
    without a browser."""

    def __init__(self, dl_dir: str, urls: Sequence[str], open_url, *, https_only: bool = False,
                 min_free_gb: float = 3.0, grace_s: float = 45.0, stall_s: float = 300.0,
                 log=print, pick=pick_source, clock=time.monotonic):
        self.dl_dir, self.urls, self.open_url = dl_dir, list(urls), open_url
        self.https_only, self.min_free_gb = https_only, min_free_gb
        self.grace_s, self.stall_s = grace_s, stall_s
        self.log, self.pick, self.clock = log, pick, clock
        self.url = ""
        self.bad: list[str] = []                   # sources that were handed over but never produced a file
        self.sizes: dict[str, int] = {}
        self.t_open: Optional[float] = None       # when the running download was asked for
        self.t_progress: Optional[float] = None   # last time a .part file grew
        self.started = self.completed = self.stalled = 0
        self.done_bytes = 0
        self._said: dict[str, float] = {}

    def _say(self, key: str, msg: str, every_s: float = 300.0) -> None:
        now = self.clock()
        if now - self._said.get(key, -1e9) >= every_s:
            self._said[key] = now
            self.log(msg)

    def parts(self) -> dict[str, int]:
        out = {}
        for p in glob.glob(os.path.join(self.dl_dir, "*.part")):
            try:
                out[p] = os.path.getsize(p)
            except OSError:
                pass
        return out

    def clean(self, everything: bool) -> int:
        """Remove finished files (all files when `everything`); bytes of the finished ones."""
        freed = 0
        for name in os.listdir(self.dl_dir):
            p = os.path.join(self.dl_dir, name)
            try:
                size = os.path.getsize(p)
                # the empty file next to a .part is the placeholder of a download that is running
                if everything or (not name.endswith(".part") and size > 0):
                    os.remove(p)
                    freed += 0 if name.endswith(".part") else size
            except OSError:
                pass
        return freed

    def free_gb(self) -> float:
        return shutil.disk_usage(self.dl_dir).free / 2 ** 30

    def _start(self, why: str) -> bool:
        self.t_open, self.t_progress = self.clock(), None      # also paces the retries (grace_s apart)
        self.sizes = {}
        if self.free_gb() < self.min_free_gb:
            self._say("disk", f"!! only {self.free_gb():.1f} GB free in {self.dl_dir} (< {self.min_free_gb:g}) - "
                              f"no new download until there is room")
            return False
        if not self.url:
            order = [u for u in self.urls if u not in self.bad] + [u for u in self.urls if u in self.bad]
            self.url, notes = self.pick(order, self.https_only)
            for n in notes:
                self._say("src:" + n, f"   source passed over - {n}")
            if not self.url:
                self._say("nosrc", "!! no download source answers - retrying; give one with --url / CF_BROWSER_DL_URL", 120)
                return False
        ok = bool(self.open_url(self.url))
        self.t_open = self.clock()
        if ok:
            self.started += 1
            self.log(f"download {self.started} started ({why}): {self.url}")
        else:
            self._say("open", "!! the browser did not take the url - retrying", 60)
        return ok

    def _drop_source(self) -> None:
        if self.url and self.url not in self.bad:
            self.bad.append(self.url)
        self.url = ""

    def tick(self) -> str:
        """One look at the directory -> what it found: downloading | waiting | started | idle."""
        now = self.clock()
        parts = self.parts()
        grew = any(size > self.sizes.get(p, -1) for p, size in parts.items())
        self.sizes = parts
        done = self.clean(everything=False) if self.started else 0    # before the first one: leftovers, removed below
        if done:
            self.completed += 1
            self.done_bytes += done
            self.log(f"download complete: {done / 2 ** 30:.2f} GB ({self.completed} so far, "
                     f"{self.done_bytes / 2 ** 30:.1f} GB in total) - removed")
        if parts:
            if grew:
                self.t_progress = now
                return "downloading"
            if now - (self.t_progress or self.t_open or now) < self.stall_s:
                if self.t_open is None:
                    self.t_open = now
                return "waiting"
            self.stalled += 1
            self.log(f"!! no progress for {self.stall_s:.0f} s - dropping the partial file and starting over")
            self.clean(everything=True)
            self._drop_source()
            return "started" if self._start("stalled") else "idle"
        if self.t_open is not None and now - self.t_open < self.grace_s and not done:
            return "waiting"                 # asked for, the .part file is not there yet
        if self.t_open is not None and not done and self.url and self.started and not self.t_progress:
            self.log(f"!! the browser took {self.url} but no file appeared in {self.grace_s:.0f} s - trying another source")
            self._drop_source()
        self.clean(everything=True)
        return "started" if self._start("first" if not self.started else "again") else "idle"


# ------------------------------------------------------------------ main
def browser_alive(proc: subprocess.Popen, profile: str) -> bool:
    if proc.poll() is None:
        return True
    from . import procs as PR                # a wrapper script may have exited after starting the browser
    import re
    return bool(PR.find_pids(r"--profile " + re.escape(profile)))


def check(urls: Sequence[str], rate_mbps: float) -> int:
    path, why = find_browser()
    print(f"browser   {path or 'MISSING'}" + (f"   ({why})" if why else ""))
    url, notes = pick_source(urls, https_only=rate_mbps > 0)
    for n in notes:
        print(f"source    passed over - {n}")
    print(f"source    {url or 'NONE ANSWERS'}")
    print(f"rate      {f'{rate_mbps:g} MB/s (through the relay)' if rate_mbps > 0 else 'unlimited (direct)'}")
    return 0 if path and url else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--app", default="browser_dl", help="app id: names the window class (cf-<app>) and the data directory")
    ap.add_argument("--url", action="append", help="file to download (repeatable; tried before the built-in mirrors)")
    ap.add_argument("--rate-mbps", type=float, default=float(os.environ.get("CF_BROWSER_DL_RATE_MBPS") or 2.0),
                    help="download speed to hold, MB/s (default 2; 0 = as fast as the network allows)")
    ap.add_argument("--dir", help="where profile/ and downloads/ live (default <data home>/<app>)")
    ap.add_argument("--min-free-gb", type=float, default=3.0, help="start no new download below this much free disk")
    ap.add_argument("--every", type=float, default=2.0, help="seconds between looks at the download directory")
    ap.add_argument("--check", action="store_true", help="report the browser and the source that would be used, then exit")
    ap.add_argument("--title", help=argparse.SUPPRESS)       # accepted for symmetry with the other bundled apps
    args = ap.parse_args(argv)
    urls = source_urls(args.url)
    if args.check:
        return check(urls, args.rate_mbps)

    def log(msg: str) -> None:
        print(f"{time.strftime('%H:%M:%S')} browser_dl[{args.app}] {msg}", flush=True)

    browser, why = find_browser()
    if not browser:
        log(f"!! {why}")
        return 3
    base = args.dir or os.path.join(data_home(), args.app)
    profile, dl_dir = os.path.join(base, "profile"), os.path.join(base, "downloads")
    relay = None
    if args.rate_mbps > 0:
        relay = Relay(args.rate_mbps * 2 ** 20)
        relay.start()
    write_profile(profile, dl_dir, relay.port if relay else 0)
    ctl = Control()
    cls = wm_class(args.app)
    base_cmd = [browser, "--name", cls, "--class", cls, "--profile", profile]
    proc = subprocess.Popen(base_cmd + [ctl.url], stdin=subprocess.DEVNULL)
    log(f"{browser} started (pid {proc.pid}, profile {profile}); downloads go to {dl_dir}, "
        + (f"held at {args.rate_mbps:g} MB/s by the relay on 127.0.0.1:{relay.port}" if relay else "not rate limited"))

    def open_url(url: str) -> bool:
        if ctl.hand(url):
            return True
        # the page is not asking (tab closed, crashed, still starting): show it again in the running browser
        log("!! the control page does not answer - opening it again")
        try:
            subprocess.run(base_cmd + [ctl.url], stdin=subprocess.DEVNULL, timeout=120)
        except (OSError, subprocess.TimeoutExpired):
            pass
        return False

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_: stop.set())
    keeper = Keeper(dl_dir, urls, open_url, https_only=relay is not None, min_free_gb=args.min_free_gb, log=log)
    keeper.clean(everything=True)
    t0 = time.monotonic()
    while ctl.t_poll is None and time.monotonic() - t0 < 120 and not stop.is_set() and browser_alive(proc, profile):
        stop.wait(0.5)                        # the page's first poll = the browser is up
    t_report = time.monotonic()
    while not stop.is_set() and browser_alive(proc, profile):
        state = keeper.tick()
        cur = sum(keeper.sizes.values())
        ctl.status = (f"{state}: 第 {keeper.started} 次下载, 当前文件 {cur / 2 ** 20:.0f} MB; 已完成 {keeper.completed} 次; "
                      f"{time.strftime('%H:%M:%S')}")
        if time.monotonic() - t_report >= 600:
            t_report = time.monotonic()
            log(f"{keeper.completed} complete + {cur / 2 ** 30:.2f} GB of the current one; {keeper.free_gb():.1f} GB free")
        stop.wait(args.every)
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(15)
        except subprocess.TimeoutExpired:
            proc.kill()
    ctl.close()
    if relay:
        relay.close()
    keeper.clean(everything=True)
    log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
