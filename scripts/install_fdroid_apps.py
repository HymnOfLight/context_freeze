#!/usr/bin/env python3
"""Download and install open-source apps from F-Droid to enlarge the Top-k set with realistic,
memory-hungry apps (browser, video, maps, ...).  Works for arm64 emulators and phones.

    python scripts/install_fdroid_apps.py                      # default list below
    python scripts/install_fdroid_apps.py org.wikipedia org.videolan.vlc
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from cf.adb import Adb  # noqa: E402

DEFAULT = ["org.mozilla.fennec_fdroid", "org.videolan.vlc", "org.schabi.newpipe",
           "app.organicmaps", "org.wikipedia", "de.danoeh.antennapod"]
API = "https://f-droid.org/api/v1/packages/{pkg}"
APK = "https://f-droid.org/repo/{pkg}_{code}.apk"
CACHE = os.path.join(os.path.dirname(__file__), "..", "results", "apk_cache")


def fetch(url: str, dest: str) -> None:
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return
    print(f"   downloading {url}")
    with urllib.request.urlopen(url, timeout=300) as r, open(dest + ".part", "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    os.replace(dest + ".part", dest)


def main(argv: list[str]) -> int:
    pkgs = argv or DEFAULT
    os.makedirs(CACHE, exist_ok=True)
    adb = Adb()
    installed = adb.shell("pm list packages")
    for pkg in pkgs:
        if f"package:{pkg}\n" in installed or f"package:{pkg}\r\n" in installed:
            print(f"== {pkg}: already installed")
            continue
        try:
            with urllib.request.urlopen(API.format(pkg=pkg), timeout=60) as r:
                meta = json.load(r)
        except Exception as e:  # noqa: BLE001
            print(f"!! {pkg}: F-Droid API failed: {e}")
            continue
        codes = [meta.get("suggestedVersionCode")] + [p["versionCode"] for p in meta.get("packages", [])]
        codes = [c for i, c in enumerate(codes) if c and c not in codes[:i]]
        ok = False
        for code in codes[:6]:   # apps with per-ABI splits publish several codes per version
            dest = os.path.join(CACHE, f"{pkg}_{code}.apk")
            try:
                fetch(APK.format(pkg=pkg, code=code), dest)
            except Exception as e:  # noqa: BLE001
                print(f"   {code}: download failed: {e}")
                continue
            out = subprocess.run([adb.adb] + (["-s", adb.serial] if adb.serial else []) +
                                 ["install", "-r", "-g", dest], capture_output=True, text=True)
            if "Success" in out.stdout:
                print(f"== {pkg}: installed versionCode {code}")
                ok = True
                break
            print(f"   {code}: {out.stdout.strip()} {out.stderr.strip()}")
            if "NO_MATCHING_ABIS" not in out.stdout + out.stderr:
                break
        if not ok:
            print(f"!! {pkg}: could not install")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
