"""Large public files to give the download apps something to download: installer images on the
Tsinghua / BFSU mirrors, found by reading the mirror's directory listings (release numbers change,
the directories do not) and checked one by one (HEAD: the file answers and says how large it is).

    python3 -m cf.linux.dl_files                             # every file of 0.7 - 3.3 GB that answers, with sizes
    python3 -m cf.linux.dl_files --rate-mbps 1 --minutes 360 # as many as an app needs at 1 MB/s for 6 hours
    python3 -m cf.linux.dl_files --gb 21 --plain             # links only, one per line (to paste into 迅雷)
    python3 -m cf.linux.dl_files --mirror https://mirrors.bfsu.edu.cn --skip 8    # other mirror, other files
    python3 -m cf.linux.dl_files --mirror https://repo.huaweicloud.com --http     # another host, plain http links: for a
                                                             # client that never gets past "looking for resources"

When the distinct files do not add up to what is asked for, the list starts over with "?n=2",
"?n=3" ... appended: the mirrors ignore the query string and serve the same file, a download
manager sees a different link.  Files are listed smallest first within each round, so that with
finished files being deleted (cf.linux.dl_trim) the disk holds one file of at most --max-gb.
"""
from __future__ import annotations

import concurrent.futures
import re
import urllib.request
from typing import Callable, Optional, Sequence

from .browser_dl import probe_source

MIRRORS = ("https://mirrors.tuna.tsinghua.edu.cn", "https://mirrors.bfsu.edu.cn", "https://repo.huaweicloud.com")
DIRS = (
    "ubuntu-releases/20.04", "ubuntu-releases/22.04", "ubuntu-releases/24.04", "ubuntu-releases/26.04",
    "debian-cd/current/amd64/iso-cd", "debian-cd/current/amd64/iso-dvd", "archlinux/iso/latest", "kali-images/current",
    "ubuntu-cdimage/xubuntu/releases/24.04/release", "ubuntu-cdimage/lubuntu/releases/24.04/release",
    "ubuntu-cdimage/ubuntu-mate/releases/24.04/release", "ubuntu-cdimage/kubuntu/releases/24.04/release",
)
_HREF = re.compile(r'href="(?:\./)?([^"/?#]+\.iso)"', re.I)


def fetch(url: str, timeout: float = 20.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux) context_freeze"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(4 * 2 ** 20).decode("utf-8", "replace")


def iso_names(html: str) -> list[str]:
    """The .iso files a directory listing links to (names only, no sub-paths), in order, once each."""
    seen: dict[str, None] = {}
    for n in _HREF.findall(html):
        seen.setdefault(n)
    return list(seen)


def collect(mirror: str, dirs: Sequence[str] = DIRS, lo_gb: float = 0.7, hi_gb: float = 3.3, *,
            get: Callable[[str], str] = fetch, probe: Callable = probe_source, workers: int = 8) -> tuple[list, list]:
    """([(url, bytes)] of the files in range that answer, smallest first; notes on what was passed
    over).  Two names of one file (archlinux-x86_64.iso and the dated one) count once."""
    urls, notes = [], []
    for d in dirs:
        try:
            names = iso_names(get(f"{mirror}/{d}/"))
        except Exception as e:                                # noqa: BLE001 - any failure: this directory is skipped
            notes.append(f"{mirror}/{d}/: {type(e).__name__}: {e}")
            continue
        urls += [f"{mirror}/{d}/{n}" for n in names]
    with concurrent.futures.ThreadPoolExecutor(max(1, workers)) as ex:
        res = list(ex.map(lambda u: probe(u, 20), urls))
    out, sizes = [], set()
    for u, (size, why) in zip(urls, res):
        if not size:
            notes.append(f"{u}: {why}")
        elif lo_gb * 2 ** 30 <= size <= hi_gb * 2 ** 30 and (u.rsplit("/", 1)[0], size) not in sizes:
            sizes.add((u.rsplit("/", 1)[0], size))
            out.append((u, size))
    out.sort(key=lambda x: (x[1], x[0]))
    return out, notes


def plain_http(files: Sequence[tuple], probe: Callable = probe_source, workers: int = 8) -> tuple[list, list]:
    """The same files as http:// links, where the server really serves them that way (no redirect
    back to https): for download clients whose TLS does not get along with the mirror."""
    urls = ["http://" + u.split("://", 1)[1] for u, _ in files]
    with concurrent.futures.ThreadPoolExecutor(max(1, workers)) as ex:
        res = list(ex.map(lambda u: probe(u, 20), urls))
    out, notes = [], []
    for u, (_, size), (got, final) in zip(urls, files, res):
        if got == size and str(final).startswith("http://"):
            out.append((u, size))
        else:
            notes.append(f"{u}: {'redirects to ' + str(final) if got else final}")
    return out, notes


def plan(files: Sequence[tuple], gb: float = 0.0, skip: int = 0, max_rounds: int = 50) -> list[tuple]:
    """The links to queue: the files from `skip` on (wrapping around), repeated with ?n=K until
    they add up to `gb` (0: every file once)."""
    if not files:
        return []
    k = skip % len(files)
    files = list(files[k:]) + list(files[:k])
    if gb <= 0:
        return files
    out, total, rnd = [], 0, 1
    while total < gb * 2 ** 30 and rnd <= max_rounds:
        for u, size in files:
            out.append((u if rnd == 1 else f"{u}?n={rnd}", size))
            total += size
            if total >= gb * 2 ** 30:
                break
        rnd += 1
    return out


def main(argv=None) -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gb", type=float, default=0.0, help="total to list (default: every file once)")
    ap.add_argument("--rate-mbps", type=float, default=0.0, help="with --minutes: list rate x time (plus 10 %%)")
    ap.add_argument("--minutes", type=float, default=0.0)
    ap.add_argument("--min-gb", type=float, default=0.7, help="smallest file to list (default 0.7)")
    ap.add_argument("--max-gb", type=float, default=3.3, help="largest file to list (default 3.3)")
    ap.add_argument("--mirror", help=f"default: the first of {', '.join(MIRRORS)} that answers")
    ap.add_argument("--skip", type=int, default=0, metavar="N", help="start with the N-th file (give two apps different files)")
    ap.add_argument("--http", action="store_true", help="plain http:// links (only the files the mirror serves that way)")
    ap.add_argument("--plain", action="store_true", help="links only")
    args = ap.parse_args(argv)
    gb = args.gb or (args.rate_mbps * args.minutes * 60 / 1024 * 1.1)
    files: list = []
    for m in ([args.mirror.rstrip("/")] if args.mirror else MIRRORS):
        files, notes = collect(m, lo_gb=args.min_gb, hi_gb=args.max_gb)
        if files and args.http:
            files, notes = plain_http(files)
        if files:
            break
        print(f"!! {m}: no file answered" + (f" ({notes[0]})" if notes else ""), file=sys.stderr)
    if not files:
        return 1
    links = plan(files, gb, args.skip)
    for u, size in links:
        print(u if args.plain else f"{size / 2 ** 30:5.2f} GB  {u}")
    sys.stdout.flush()
    total = sum(s for _, s in links) / 2 ** 30
    print(f"{len(links)} links, {total:.1f} GB in total ({len(files)} different files, {sum(s for _, s in files) / 2 ** 30:.1f} GB); "
          f"largest {max(s for _, s in links) / 2 ** 30:.2f} GB"
          + (f"; asked for {gb:.1f} GB" if gb else ""), file=sys.stderr)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
