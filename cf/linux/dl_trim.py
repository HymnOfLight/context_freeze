"""Keep the download apps from filling the disk while they run: delete what they finished and,
optionally, free what they have written so far.

百度网盘 and 迅雷 really write what they download, so a long run needs rate x time of disk.  The
experiment only needs the apps to keep receiving and writing; nobody reads the files afterwards.
This watcher runs next to the experiment, outside the apps' cgroups (its CPU time is not charged
to them), and looks every few seconds at the files the apps' processes have open:

  finished files   a file that an app of the experiment was seen writing (64 MB or larger, not in a
                   hidden or system directory), that no process of the app has open any more for
                   `--grace` seconds and that no longer has a temporary download name (.xltd,
                   .downloading, .part ...) is complete.  It is deleted.  The file is followed by
                   its inode, so the rename at the end of a download does not lose it, and files
                   the watcher never saw being written - whatever else is in that directory - are
                   never touched.  Disk needed: the files in progress, not rate x time.
  --punch          additionally give back the blocks of the files still being written (fallocate
                   PUNCH_HOLE over the whole file, every look).  The file keeps its size and the
                   app keeps writing at its offset; the disk holds only what arrived since the last
                   look.  What was written reads back as zeros afterwards, so an app that verifies
                   the finished file will find it damaged: try it on one task before a long run.

    python3 -m cf.linux.dl_trim CONFIG --list           # which files the download apps are writing right now
    python3 -m cf.linux.dl_trim CONFIG --dry-run        # watch and say what would be deleted, delete nothing
    python3 -m cf.linux.dl_trim CONFIG [--punch]        # run until stopped (run_classes.sh: DL_TRIM=1 | punch)

An app that manages its own files (cf.linux.browser_dl deletes what it finished) is left alone,
except that --punch also frees its partial file.
"""
from __future__ import annotations

import ctypes
import os
import signal
import stat
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

TEMP_SUFFIXES = (".xltd", ".td", ".downloading", ".part", ".crdownload", ".partial", ".tmp", ".temp", ".bc!",
                 ".cfg", ".aria2", ".download")
SYSTEM_DIRS = ("/usr/", "/opt/", "/var/", "/tmp/", "/proc/", "/sys/", "/dev/", "/run/", "/snap/", "/etc/", "/boot/",
               "/lib/", "/bin/", "/sbin/")
FALLOC_FL_KEEP_SIZE, FALLOC_FL_PUNCH_HOLE = 1, 2
DELETED = " (deleted)"


@dataclass
class OpenFile:
    pid: int
    fd: int
    path: str
    dev: int
    ino: int
    size: int            # apparent size
    disk: int            # bytes actually allocated
    writable: bool

    @property
    def key(self) -> tuple[int, int]:
        return (self.dev, self.ino)


def open_files(pid: int, proc: str = "/proc", want: Optional[Callable[[str], bool]] = None) -> list[OpenFile]:
    """The regular files a process has open (`want(path)` filters before the file is looked at)."""
    out = []
    d = f"{proc}/{pid}/fd"
    try:
        names = os.listdir(d)
    except OSError:
        return out
    for n in names:
        link = f"{d}/{n}"
        try:
            path = os.readlink(link)
            if not path.startswith("/") or (want is not None and not want(path)):
                continue                                   # socket:[...], pipe:[...], anon_inode
            st = os.stat(link)
            if not stat.S_ISREG(st.st_mode):
                continue
            flags = 0
            with open(f"{proc}/{pid}/fdinfo/{n}") as f:
                for line in f:
                    if line.startswith("flags:"):
                        flags = int(line.split()[1], 8)
                        break
        except (OSError, ValueError):
            continue
        out.append(OpenFile(pid, int(n), path, st.st_dev, st.st_ino, st.st_size, st.st_blocks * 512,
                            bool(flags & (os.O_WRONLY | os.O_RDWR))))
    return out


def is_temp(name: str) -> bool:
    """A name download managers give a file that is not complete yet."""
    low = name.lower()
    return low.endswith(TEMP_SUFFIXES)


def has_sidecar(path: str) -> bool:
    """A companion file next to it that says "in progress" (x.iso.td.cfg, x.iso.aria2 ...)."""
    d, name = os.path.split(path)
    try:
        return any(e != name and e.startswith(name) and is_temp(e) for e in os.listdir(d))
    except OSError:
        return False


def clean_path(path: str) -> str:
    return path[:-len(DELETED)] if path.endswith(DELETED) else path


def plausible(path: str, dirs: Sequence[str] = ()) -> bool:
    """Could this be a downloaded file?  Not in a hidden directory (profiles, caches, databases)
    and not in a system one - unless it lies under one of `dirs`, which are named explicitly."""
    p = clean_path(path)
    if any(p.startswith(d.rstrip("/") + "/") for d in dirs):
        return True
    if p.startswith(SYSTEM_DIRS):
        return False
    return not any(part.startswith(".") for part in p.split("/")[1:-1])


def punch(path: str, size: int) -> Optional[str]:
    """Give the blocks of [0, size) back to the filesystem; the file keeps its length.  None, or
    why it did not work (a filesystem without hole punching)."""
    try:
        fd = os.open(path, os.O_WRONLY)
    except OSError as e:
        return str(e)
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.fallocate.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int64, ctypes.c_int64]
        if libc.fallocate(fd, FALLOC_FL_PUNCH_HOLE | FALLOC_FL_KEEP_SIZE, 0, size) != 0:
            return os.strerror(ctypes.get_errno())
        return None
    finally:
        os.close(fd)


@dataclass
class Item:
    app: str
    key: tuple[int, int]
    path: str
    size: int
    t_open: float                      # when a process of the app last had it open
    paused: bool = False               # closed, but still has an in-progress name: said once


@dataclass
class AppRule:
    delete: bool = True                # False: the app removes its own files; only --punch applies
    dirs: tuple = field(default_factory=tuple)      # files under these count even in a hidden directory


class Trimmer:
    """`groups()` -> {app: [pids]}; `tick()` looks once."""

    def __init__(self, groups: Callable[[], dict], rules: Optional[dict] = None, *, min_mb: float = 64.0,
                 grace_s: float = 30.0, punch_files: bool = False, dry: bool = False, log: Callable[[str], None] = print,
                 clock: Callable[[], float] = time.monotonic, opener: Callable = open_files, proc: str = "/proc"):
        self.groups, self.rules = groups, dict(rules or {})
        self.min_bytes, self.grace_s = int(min_mb * 2 ** 20), grace_s
        self.punch_files, self.dry, self.log, self.clock, self.opener, self.proc = punch_files, dry, log, clock, opener, proc
        self.items: dict[tuple[int, int], Item] = {}
        self.removed = 0               # files
        self.removed_bytes = 0         # their length (with --punch little of it was still on the disk)
        self.punched_bytes = 0
        self.punch_error: Optional[str] = None

    def rule(self, app: str) -> AppRule:
        return self.rules.get(app) or AppRule()

    # ----------------------------------------------------------- one look
    def scan(self) -> dict[tuple[int, int], tuple[str, OpenFile]]:
        """Every plausible regular file the apps have open: key -> (app, the open file; a writable
        descriptor wins over a read-only one)."""
        seen: dict[tuple[int, int], tuple[str, OpenFile]] = {}
        for app, pids in self.groups().items():
            dirs = self.rule(app).dirs
            for pid in pids:
                for f in self.opener(pid, self.proc, lambda p, d=dirs: plausible(p, d)):
                    cur = seen.get(f.key)
                    if cur is None or (f.writable and not cur[1].writable):
                        seen[f.key] = (app, f)
        return seen

    def tick(self) -> dict:
        now = self.clock()
        seen = self.scan()
        for key, (app, f) in seen.items():
            it = self.items.get(key)
            if it is None:
                if not f.writable or f.size < self.min_bytes:
                    continue
                it = self.items[key] = Item(app, key, clean_path(f.path), f.size, now)
                self.log(f"{app}: writing {it.path} ({f.size / 2 ** 30:.2f} GB so far)")
            it.path, it.size, it.t_open, it.paused = clean_path(f.path), f.size, now, False
            if self.punch_files and f.writable and f.disk >= 8 * 2 ** 20:
                self._punch(it, f)
        for key, it in list(self.items.items()):
            if key not in seen and now - it.t_open >= self.grace_s:
                self._closed(it)
        return {"watching": len(self.items), "removed": self.removed, "removed_gb": self.removed_bytes / 2 ** 30,
                "punched_gb": self.punched_bytes / 2 ** 30}

    def _punch(self, it: Item, f: OpenFile) -> None:
        if self.dry:
            return
        why = punch(f"{self.proc}/{f.pid}/fd/{f.fd}", f.size)      # by descriptor: the path may have been renamed
        if why:
            if why != self.punch_error:
                self.punch_error = why
                self.log(f"!! {it.app}: cannot free the written part of {it.path}: {why}")
            return
        try:
            left = os.stat(f"{self.proc}/{f.pid}/fd/{f.fd}").st_blocks * 512
        except OSError:
            left = 0
        self.punched_bytes += max(0, f.disk - left)

    def locate(self, it: Item) -> Optional[str]:
        """Where the file is now: its last known path, or the entry of that directory with the
        same inode (the app renamed it when the download ended)."""
        try:
            st = os.stat(it.path)
            if (st.st_dev, st.st_ino) == it.key:
                return it.path
        except OSError:
            pass
        d = os.path.dirname(it.path)
        try:
            for e in os.scandir(d):
                if e.inode() == it.key[1] and e.is_file(follow_symlinks=False) and e.stat(follow_symlinks=False).st_dev == it.key[0]:
                    return e.path
        except OSError:
            pass
        return None

    def _closed(self, it: Item) -> None:
        path = self.locate(it)
        if path is None:
            del self.items[it.key]                      # the app removed it or moved it elsewhere
            return
        if is_temp(os.path.basename(path)) or has_sidecar(path):
            if not it.paused:
                it.paused = True
                self.log(f"{it.app}: {path} is closed but not complete (paused?) - left alone")
            return
        del self.items[it.key]
        if not self.rule(it.app).delete:
            return
        try:
            st = os.stat(path)
            if not self.dry:
                os.remove(path)
        except OSError as e:
            self.log(f"!! {it.app}: cannot remove {path}: {e}")
            return
        self.removed += 1
        self.removed_bytes += st.st_size
        self.log(f"{it.app}: finished {path} ({st.st_size / 2 ** 30:.2f} GB) - "
                 f"{'would be removed (dry run)' if self.dry else 'removed'}; {self.removed} files, "
                 f"{self.removed_bytes / 2 ** 30:.1f} GB so far")

    def summary(self) -> str:
        s = f"{self.removed} finished files removed ({self.removed_bytes / 2 ** 30:.1f} GB)"
        if self.punch_files:
            s += f", {self.punched_bytes / 2 ** 30:.1f} GB of files in progress given back"
        return s + f"; {len(self.items)} files being followed"


# ------------------------------------------------------------------ CLI
def listing(groups: dict, rules: dict, min_mb: float, opener: Callable = open_files, proc: str = "/proc") -> list[str]:
    """What --list prints: per app, the files it is writing that look like downloads."""
    lines = []
    for app, pids in groups.items():
        dirs = (rules.get(app) or AppRule()).dirs
        files: dict[tuple[int, int], OpenFile] = {}
        for pid in pids:
            for f in opener(pid, proc, lambda p, d=dirs: plausible(p, d)):
                if f.writable and f.size >= 2 ** 20:
                    files[f.key] = f
        lines.append(f"{app}: {len(pids)} processes" + ("" if pids else "  (not running, or not started through the runner)"))
        for f in sorted(files.values(), key=lambda f: -f.size):
            mark = "followed" if f.size >= min_mb * 2 ** 20 else f"ignored: under {min_mb:g} MB"
            lines.append(f"    {f.size / 2 ** 30:7.2f} GB ({f.disk / 2 ** 30:.2f} on disk)  {clean_path(f.path)}   [{mark}]")
        if pids and not files:
            lines.append("    no file being written (no download running, or it writes into a hidden directory: --dir)")
    return lines


def main(argv=None) -> int:
    import argparse
    import json

    from . import apps as A
    from . import browser_dl
    from .device import LinuxDesktop
    from .onboard import self_managed

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("config")
    ap.add_argument("--apps", nargs="+", help="default: the config's classes.download apps that are real apps")
    ap.add_argument("--list", action="store_true", help="show the files the apps are writing now and exit")
    ap.add_argument("--punch", action="store_true", help="also free the written part of files in progress (see above)")
    ap.add_argument("--dry-run", action="store_true", help="say what would be removed / freed, change nothing")
    ap.add_argument("--every", type=float, default=15.0, metavar="S", help="seconds between looks (default 15)")
    ap.add_argument("--grace", type=float, default=30.0, metavar="S",
                    help="a file counts as finished when no process of the app had it open for this long (default 30)")
    ap.add_argument("--min-mb", type=float, default=64.0, help="files smaller than this are not followed (default 64)")
    ap.add_argument("--dir", action="append", default=[], metavar="D",
                    help="download directory that is hidden or under a system path (repeatable)")
    ap.add_argument("--for", dest="for_s", type=float, default=0.0, metavar="S", help="stop after S seconds (default: run until killed)")
    args = ap.parse_args(argv)
    cfg = json.load(open(args.config))
    cat = A.with_overrides(cfg.get("app_overrides"), cfg.get("placeholder_apps"))
    dev = LinuxDesktop(cat=cat, notifications=False)
    ids = args.apps or [a for a in (cfg.get("classes") or {}).get("download") or []
                        if a in cat and cat[a].kind not in ("bench", "placeholder") and dev.is_installed(cat[a])]
    extra = tuple(os.path.abspath(os.path.expanduser(d)) for d in args.dir)
    rules = {}
    for a in ids:
        if self_managed(cat[a]):
            rules[a] = AppRule(delete=False, dirs=extra + (os.path.join(browser_dl.data_home(), a, "downloads"),))
        else:
            rules[a] = AppRule(dirs=extra)
    if not args.punch:
        ids = [a for a in ids if rules[a].delete]        # nothing to do for an app that removes its own files
    if not ids:
        print("no real download apps (installed) in this config that leave files behind")
        return 0

    def groups() -> dict:
        return {a: sorted(set(dev.pids(a)) | set(dev.stray_pids(a))) for a in ids}

    if args.list:
        print("\n".join(listing(groups(), rules, args.min_mb)))
        return 0

    def log(msg: str) -> None:
        print(f"{time.strftime('%H:%M:%S')} dl_trim {msg}", flush=True)

    def stop(*_):
        raise SystemExit(0)
    for s in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(s, stop)
    t = Trimmer(groups, rules, min_mb=args.min_mb, grace_s=args.grace, punch_files=args.punch, dry=args.dry_run, log=log)
    what = [f"finished files are {'reported' if args.dry_run else 'removed'}"] if any(rules[a].delete for a in ids) else []
    if args.punch:
        what.append("the written part of files in progress is given back" + (" (dry run: not really)" if args.dry_run else ""))
    log(f"watching {', '.join(ids)}: {', '.join(what)}"
        + f" (every {args.every:g} s, grace {args.grace:g} s, files of {args.min_mb:g} MB and more)")
    t0 = t_said = time.monotonic()
    try:
        while not args.for_s or time.monotonic() - t0 < args.for_s:
            t.tick()
            if time.monotonic() - t_said >= 600:
                t_said = time.monotonic()
                free = os.statvfs(os.path.expanduser("~"))
                log(f"{t.summary()}; {free.f_bavail * free.f_frsize / 2 ** 30:.1f} GB free")
            time.sleep(args.every)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        log(f"stopped: {t.summary()}")
        dev.close()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
