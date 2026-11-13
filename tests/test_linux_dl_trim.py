"""Deleting what the download apps finished while they run (cf.linux.dl_trim) and the list of public
test files to feed them with (cf.linux.dl_files)."""
import os
import subprocess
import sys

import pytest

from cf.linux import dl_files as F
from cf.linux import dl_trim as T

MB = 2 ** 20

# a stand-in for a download client, driven line by line over stdin:
#   open PATH | write MB | rename NEW | close | read PATH (keeps it open read-only)
CLIENT = r"""
import os, sys
f = None; path = None; keep = []
for line in sys.stdin:
    cmd, _, arg = line.strip().partition(" ")
    if cmd == "open":
        path = arg; f = open(arg, "wb", buffering=0)
    elif cmd == "write":
        f.write(os.urandom(int(arg) * 2 ** 20)); os.fsync(f.fileno())
    elif cmd == "rename":
        os.rename(path, arg); path = arg
    elif cmd == "close":
        f.close(); f = None
    elif cmd == "read":
        keep.append(open(arg, "rb"))
    print("ok", flush=True)
"""


class Client:
    def __init__(self):
        self.p = subprocess.Popen([sys.executable, "-c", CLIENT], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.pid = self.p.pid

    def do(self, line):
        self.p.stdin.write(line + "\n")
        self.p.stdin.flush()
        assert self.p.stdout.readline().strip() == "ok"

    def stop(self):
        self.p.stdin.close()
        self.p.wait(10)


@pytest.fixture
def client():
    c = Client()
    yield c
    if c.p.poll() is None:
        c.p.kill()


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


def _trimmer(tmp_path, pids, **kw):
    log, clock = [], Clock()
    rules = kw.pop("rules", {"xunlei": T.AppRule(dirs=(str(tmp_path),))})      # pytest's tmp dir is under /tmp
    t = T.Trimmer(lambda: {"xunlei": list(pids)}, rules, min_mb=1, grace_s=30, log=log.append, clock=clock, **kw)
    return t, clock, log


# ------------------------------------------------------------------ what counts as a download
def test_only_plausible_paths_and_finished_names():
    assert T.plausible("/home/u/Downloads/a.iso") and T.plausible("/data/dl/a.iso (deleted)")
    assert not T.plausible("/home/u/.config/xunlei/task.db")           # profile / cache / database
    assert not T.plausible("/home/u/.cache/mozilla/x/cache2/entries/AB12")
    assert not T.plausible("/opt/apps/com.xunlei.download/files/log.txt") and not T.plausible("/tmp/x.iso")
    assert T.plausible("/home/u/.local/share/context_freeze/browser_dl/downloads/a.iso.part",
                       dirs=("/home/u/.local/share/context_freeze/browser_dl/downloads",))
    for n in ("a.iso.xltd", "a.iso.baiduyun.p.downloading", "a.Ab12.iso.part", "a.iso.crdownload", "A.ISO.TD", "a.iso.td.cfg"):
        assert T.is_temp(n), n
    assert not T.is_temp("a.iso") and not T.is_temp("part.iso") and not T.is_temp("a.xltd.iso")


def test_open_files_tells_writing_from_reading(tmp_path, client):
    (tmp_path / "old.bin").write_bytes(b"x" * 10)
    client.do(f"open {tmp_path}/new.bin")
    client.do("write 1")
    client.do(f"read {tmp_path}/old.bin")
    files = {os.path.basename(f.path): f for f in T.open_files(client.pid, want=lambda p: p.startswith(str(tmp_path)))}
    assert set(files) == {"new.bin", "old.bin"}
    assert files["new.bin"].writable and files["new.bin"].size == MB and files["new.bin"].disk >= MB
    assert not files["old.bin"].writable
    assert T.open_files(999999999) == []


# ------------------------------------------------------------------ finished files
def test_a_finished_download_is_removed_and_nothing_else(tmp_path, client):
    mine = tmp_path / "holiday.iso"                              # in the same directory, not written by the app now
    mine.write_bytes(b"m" * 3 * MB)
    seeded = tmp_path / "seeding.iso"                            # only read by the app (an upload): never followed
    seeded.write_bytes(b"s" * 2 * MB)
    t, clock, log = _trimmer(tmp_path, [client.pid])
    client.do(f"read {seeded}")
    client.do(f"open {tmp_path}/a.iso.xltd")
    assert t.tick()["watching"] == 0                             # empty so far: under the size that is followed
    client.do("write 2")
    assert t.tick()["watching"] == 1 and "writing" in log[-1]
    clock.t += 100
    assert t.tick()["removed"] == 0                              # still open: in progress however long it takes
    client.do("close")                                           # paused: closed, but the name says "not complete"
    t.tick()
    clock.t += 100
    assert t.tick()["removed"] == 0 and (tmp_path / "a.iso.xltd").exists() and "not complete" in log[-1]
    n_log = len(log)
    clock.t += 100
    t.tick()
    assert len(log) == n_log                                     # said once
    os.rename(tmp_path / "a.iso.xltd", tmp_path / "a.iso")       # the client finishes it: found again by its inode
    clock.t += 5
    assert t.tick() == {"watching": 0, "removed": 1, "removed_gb": pytest.approx(2 / 1024, rel=0.05), "punched_gb": 0.0}
    assert not (tmp_path / "a.iso").exists() and "removed" in log[-1]
    assert mine.exists() and seeded.exists()


def test_a_file_is_not_finished_until_the_grace_time_is_over(tmp_path, client):
    t, clock, log = _trimmer(tmp_path, [client.pid])
    client.do(f"open {tmp_path}/b.iso.downloading")
    client.do("write 2")
    t.tick()
    client.do(f"rename {tmp_path}/b.iso")                        # renamed while open: the path follows
    t.tick()
    assert t.items[next(iter(t.items))].path == str(tmp_path / "b.iso")
    client.do("close")
    clock.t += 20
    t.tick()
    assert (tmp_path / "b.iso").exists()                         # closed 20 s ago: the client may open it again (verify, move)
    clock.t += 15
    t.tick()
    assert not (tmp_path / "b.iso").exists() and t.removed == 1


def test_a_final_name_with_a_progress_file_next_to_it_is_left_alone(tmp_path, client):
    """Older clients write straight into the final name and keep the state in x.td.cfg / x.aria2."""
    t, clock, log = _trimmer(tmp_path, [client.pid])
    client.do(f"open {tmp_path}/c.iso")
    client.do("write 2")
    (tmp_path / "c.iso.aria2").write_bytes(b"state")
    t.tick()
    client.do("close")
    clock.t += 60
    t.tick()
    assert (tmp_path / "c.iso").exists() and t.removed == 0 and T.has_sidecar(str(tmp_path / "c.iso"))
    os.remove(tmp_path / "c.iso.aria2")                          # done
    clock.t += 1
    t.tick()
    assert not (tmp_path / "c.iso").exists()


def test_dry_run_and_apps_that_remove_their_own_files(tmp_path, client):
    t, clock, log = _trimmer(tmp_path, [client.pid], dry=True)
    client.do(f"open {tmp_path}/d.iso")
    client.do("write 2")
    t.tick()
    client.do("close")
    clock.t += 31
    t.tick()
    assert (tmp_path / "d.iso").exists() and t.removed == 1 and "would be removed" in log[-1] and not t.items

    t, clock, log = _trimmer(tmp_path, [client.pid], rules={"xunlei": T.AppRule(delete=False, dirs=(str(tmp_path),))})
    client.do(f"open {tmp_path}/e.iso")
    client.do("write 2")
    t.tick()
    client.do("close")
    clock.t += 31
    t.tick()
    assert (tmp_path / "e.iso").exists() and t.removed == 0 and not t.items


def test_files_the_app_removed_itself_or_hidden_ones_are_no_problem(tmp_path, client):
    t, clock, log = _trimmer(tmp_path, [client.pid])
    client.do(f"open {tmp_path}/f.iso")
    client.do("write 2")
    t.tick()
    client.do("close")
    os.remove(tmp_path / "f.iso")
    clock.t += 31
    assert t.tick() == {"watching": 0, "removed": 0, "removed_gb": 0.0, "punched_gb": 0.0}
    assert not any(m.startswith("!!") for m in log)

    t, clock, log = _trimmer(tmp_path, [client.pid], rules={})   # no directory named: /tmp/... is a system path
    client.do(f"open {tmp_path}/g.iso")
    client.do("write 2")
    assert t.tick()["watching"] == 0
    assert T.listing({"xunlei": [client.pid]}, {}, 1)[-1].lstrip().startswith("no file being written")
    lines = T.listing({"xunlei": [client.pid], "baidunetdisk": []}, {"xunlei": T.AppRule(dirs=(str(tmp_path),))}, 64)
    assert "1 processes" in lines[0] and "g.iso" in lines[1] and "followed once it reaches 64 MB" in lines[1]
    assert "not running" in lines[-1]


# ------------------------------------------------------------------ files in progress
def test_punching_frees_the_written_part_and_the_app_keeps_writing(tmp_path, client):
    path = tmp_path / "h.iso.xltd"
    client.do(f"open {path}")
    client.do("write 20")
    scratch = tmp_path / "scratch"
    scratch.write_bytes(os.urandom(MB))
    if T.punch(str(scratch), MB) is not None or os.stat(path).st_blocks == 0:
        pytest.skip("this filesystem does not account blocks / punch holes")
    t, clock, log = _trimmer(tmp_path, [client.pid], punch_files=True)
    r = t.tick()
    st = os.stat(path)
    assert st.st_size == 20 * MB and st.st_blocks * 512 < MB          # same length, nothing left on disk
    assert r["punched_gb"] == pytest.approx(20 / 1024, rel=0.1) and r["removed"] == 0
    client.do("write 12")                                             # the client goes on at its offset
    st = os.stat(path)
    assert st.st_size == 32 * MB and 11 * MB < st.st_blocks * 512 < 14 * MB
    with open(path, "rb") as f:
        assert f.read(4096) == b"\0" * 4096                           # what was freed reads back as zeros
        f.seek(21 * MB)
        assert f.read(4096) != b"\0" * 4096
    t.tick()
    assert os.stat(path).st_blocks * 512 < MB and "given back" in t.summary()

    t2, _, _ = _trimmer(tmp_path, [client.pid], punch_files=True, dry=True)
    client.do("write 9")
    t2.tick()
    assert os.stat(path).st_blocks * 512 > 8 * MB                     # dry run: untouched


# ------------------------------------------------------------------ test files to download
LISTING = """<html><a href="../">Parent</a>
<a href="ubuntu-9.04-live-server-amd64.iso">ubuntu-9.04-live-server-amd64.iso</a>
<a href="./ubuntu-9.04-desktop-amd64.iso" title="x">desktop</a>
<a href="ubuntu-9.04-live-server-amd64.iso">again</a>
<a href="ubuntu-9.04-desktop-amd64.iso.zsync">zsync</a> <a href="sub/other.iso">other</a> <a href="SHA256SUMS">sums</a></html>"""


def test_iso_names_reads_a_directory_listing():
    assert F.iso_names(LISTING) == ["ubuntu-9.04-live-server-amd64.iso", "ubuntu-9.04-desktop-amd64.iso"]


def test_collect_keeps_the_files_that_answer_and_fit():
    GB = 2 ** 30
    pages = {"https://m/a/": '<a href="s.iso">s</a><a href="big.iso">b</a><a href="gone.iso">g</a>',
             "https://m/b/": '<a href="arch-2026.iso">x</a><a href="arch-latest.iso">x</a><a href="tiny.iso">t</a>'}
    sizes = {"https://m/a/s.iso": 2 * GB, "https://m/a/big.iso": 6 * GB, "https://m/b/arch-2026.iso": GB,
             "https://m/b/arch-latest.iso": GB, "https://m/b/tiny.iso": GB // 10}

    def get(url):
        if url not in pages:
            raise OSError("404")
        return pages[url]

    def probe(url, timeout):
        return (sizes[url], url) if url in sizes else (None, "HTTP 404")
    files, notes = F.collect("https://m", ["a", "b", "c"], 0.7, 3.3, get=get, probe=probe)
    assert files == [("https://m/b/arch-2026.iso", GB), ("https://m/a/s.iso", 2 * GB)]     # smallest first, the alias once
    assert any("gone.iso" in n and "404" in n for n in notes) and any(n.startswith("https://m/c/") for n in notes)


def test_plan_repeats_the_files_with_a_query_string_until_it_is_enough():
    GB = 2 ** 30
    files = [("u1", GB), ("u2", 2 * GB), ("u3", 3 * GB)]
    assert F.plan(files) == files and F.plan([], 5) == []
    assert F.plan(files, 2.5) == [("u1", GB), ("u2", 2 * GB)]
    assert [u for u, _ in F.plan(files, 8)] == ["u1", "u2", "u3", "u1?n=2", "u2?n=2"]
    assert [u for u, _ in F.plan(files, 4, skip=1)] == ["u2", "u3"]                      # a second app gets other files first
    assert [u for u, _ in F.plan(files, 0, skip=4)] == ["u2", "u3", "u1"]


def test_run_script_starts_and_stops_the_trimmer():
    sh = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts/ubuntu/run_classes.sh")).read()
    assert 'DL_TRIM="${DL_TRIM:-0}"' in sh and 'kill "$TRIM_PID"' in sh and "cf.linux.dl_trim" in sh


def test_plain_http_keeps_only_what_the_mirror_really_serves_over_http():
    GB = 2 ** 30
    files = [("https://m/a.iso", GB), ("https://m/b.iso", 2 * GB), ("https://m/c.iso", GB)]

    def probe(url, timeout):
        return {"http://m/a.iso": (GB, "http://m/a.iso"),                     # served as asked
                "http://m/b.iso": (2 * GB, "https://m/b.iso"),                # sent back to https
                "http://m/c.iso": (None, "HTTP 403")}[url]
    out, notes = F.plain_http(files, probe=probe)
    assert out == [("http://m/a.iso", GB)]
    assert any("redirects to https://m/b.iso" in n for n in notes) and any("403" in n for n in notes)


# ------------------------------------------------------------------ fewer download apps
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_subset_reduces_the_download_class_and_the_visiting_order():
    import json

    from cf.linux import subset as S
    cfg = json.load(open(os.path.join(REPO, "configs/ubuntu_arm64_full.json")))
    out, gone = S.subset(cfg, download=["xunlei"])
    assert gone == ["baidunetdisk", "browser_dl"]
    assert out["classes"]["download"] == ["xunlei"] and out["classes"]["compute"] == cfg["classes"]["compute"]
    assert "baidunetdisk" not in out["apps"] and "browser_dl" not in out["apps"] and len(out["apps"]) == 11
    pat = out["trace"]["pattern"]
    assert len(pat) == 15 and pat.count("xunlei") == 1 and "browser_dl" not in pat
    assert all(a != b for a, b in zip(pat, pat[1:])) and pat[0] != pat[-1]
    assert out["trace"]["T"] == 30 and out["throttle"] == cfg["throttle"]
    assert cfg["classes"]["download"] == ["baidunetdisk", "xunlei", "browser_dl"]          # the original is not touched

    two, gone = S.subset(cfg, download=["xunlei", "browser_dl"])                           # 迅雷 + the Firefox download
    assert gone == ["baidunetdisk"] and two["classes"]["download"] == ["xunlei", "browser_dl"]
    assert len(two["apps"]) == 12 and len(two["trace"]["pattern"]) == 16 and two["trace"]["T"] == 32
    assert two["trace"]["pattern"].count("browser_dl") == 1 and two["trace"]["pattern"].count("firefox") == 2

    none, gone = S.subset(cfg, download=[])
    assert none["classes"]["download"] == [] and len(none["trace"]["pattern"]) == 14 and len(gone) == 3
    same, gone = S.subset(cfg, download=["baidunetdisk", "xunlei", "browser_dl"])
    assert gone == [] and same == cfg
    with pytest.raises(ValueError, match="not a download app"):
        S.subset(cfg, download=["thunder"])
    with pytest.raises(ValueError, match="not an app"):
        S.subset(cfg, drop=["nope"])

    toy = {"apps": ["a", "b", "c"], "classes": {"interactive": ["a", "c"], "download": ["b"]},
           "trace": {"kind": "pattern", "pattern": ["a", "b", "a", "c", "b"]}}
    out, _ = S.subset(toy, drop=["b"])
    assert out["trace"]["pattern"] == ["a", "c"]               # a b a -> one visit to a; the trailing b goes


def test_run_script_runs_the_reduced_config(tmp_path):
    import json
    env = dict(os.environ, DRY="1", OUT=str(tmp_path), CONFIG="configs/ubuntu_arm64_full.json", DL_APPS="xunlei", ROUNDS="6",
               SEEDS="1 2 3")
    r = subprocess.run(["bash", "scripts/ubuntu/run_classes.sh"], cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    cfg = json.load(open(tmp_path / "config.json"))
    assert cfg["classes"]["download"] == ["xunlei"]
    assert "rounds=6 (T=90)" in r.stdout and f"config={tmp_path}/config.json" in r.stdout
    cmds = [ln for ln in r.stdout.splitlines() if ln.startswith("python3 -u run_linux.py")]
    assert len(cmds) == 9 and all(f"{tmp_path}/config.json" in c and "--T 90" in c for c in cmds)

    env["DL_APPS"] = "thunder"
    r = subprocess.run(["bash", "scripts/ubuntu/run_classes.sh"], cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 2 and "not a download app" in r.stdout + r.stderr


def test_limits_left_on_an_app_are_named(tmp_path):
    from cf.linux import netstat as N
    from cf.linux.cgroup import CgroupTree
    tree = CgroupTree(root=str(tmp_path), subdir="cf")
    assert N.limits_now(tree, "xunlei") == ""                   # no such cgroup
    leaf = tmp_path / "cf" / "xunlei"
    leaf.mkdir(parents=True)
    for name, text in (("cpu.max", "max 100000\n"), ("memory.high", "max\n"), ("cgroup.freeze", "0\n")):
        (leaf / name).write_text(text)
    assert N.limits_now(tree, "xunlei") == ""
    (leaf / "cpu.max").write_text("10000 100000\n")
    (leaf / "memory.high").write_text(str(32 * 2 ** 20) + "\n")
    assert N.limits_now(tree, "xunlei") == "cpu.max 10 % of a core, memory.high 32 MB"
