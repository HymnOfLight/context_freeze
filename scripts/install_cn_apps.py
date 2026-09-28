#!/usr/bin/env python3
"""Install the mainland-China app set (cf.scenarios.CATALOG) on the emulator.

The apps are not on Google Play and the vendors do not offer stable direct-download URLs, so the
APKs have to come from one of two places:

  1. a local directory (default apks/): *.apk, or *.xapk / *.apks / *.zip bundles with split APKs
     (base.apk + split_config.*.apk), downloaded from the official pages listed by --list;

  2. a phone you own, connected over adb next to the emulator (--from-phone <serial>): the
     installed APKs (base + splits) of every catalogue package are pulled with `pm path` and
     installed on the emulator. Phones are arm64 like the emulator, so the native libraries match.

    python3 scripts/install_cn_apps.py --list                    # catalogue + download pages + installed?
    python3 scripts/install_cn_apps.py                           # install everything found in apks/
    python3 scripts/install_cn_apps.py --dir ~/Downloads/apks    # another directory
    python3 scripts/install_cn_apps.py --from-phone 1234ABCD     # pull from a phone, then install
    python3 scripts/install_cn_apps.py --from-phone 1234ABCD com.tencent.mm com.sina.weibo

Afterwards run scripts/first_run_cn_apps.sh once: these apps show a privacy agreement and often a
login screen on first start, which must be dealt with by hand before the experiments.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from cf.adb import Adb, AdbError  # noqa: E402
from cf.scenarios import CATALOG, display_name  # noqa: E402

DEFAULT_DIR = os.path.join(os.path.dirname(__file__), "..", "apks")


def installed_packages(adb: Adb) -> set[str]:
    out = adb.shell("pm list packages")
    return {l.strip()[len("package:"):] for l in out.splitlines() if l.strip().startswith("package:")}


def emulator_abi(adb: Adb) -> str:
    return adb.shell("getprop ro.product.cpu.abi").strip()


def install_files(adb: Adb, files: list[str]) -> bool:
    """`adb install -r -g` for one APK, `install-multiple` for base + splits. -g grants all runtime
    permissions up front so the first start shows fewer dialogs."""
    cmd = ["install-multiple", "-r", "-g"] if len(files) > 1 else ["install", "-r", "-g"]
    try:
        out = adb.run(*cmd, *files, timeout=900)
    except AdbError as e:
        msg = str(e)
        if "INSTALL_FAILED_NO_MATCHING_ABIS" in msg:
            print(f"   !! native libraries do not match the emulator ABI ({emulator_abi(adb)}): "
                  f"this APK is for another architecture (x86 image + arm-only app, or vice versa)")
        elif "INSTALL_FAILED_UPDATE_INCOMPATIBLE" in msg:
            print("   !! a differently signed version is installed; `adb uninstall <pkg>` first")
        else:
            print(f"   !! install failed: {msg.strip().splitlines()[-1] if msg.strip() else e}")
        return False
    return "Success" in out


def bundle_members(path: str, tmp: str) -> list[str]:
    """Extract the APKs from an .xapk/.apks/.zip bundle (base.apk + split_*.apk; OBB files are ignored)."""
    out = []
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if name.lower().endswith(".apk") and "/" not in name.strip("/"):
                z.extract(name, tmp)
                out.append(os.path.join(tmp, name))
    # base first, then splits
    out.sort(key=lambda p: (0 if os.path.basename(p) in ("base.apk",) or "split" not in os.path.basename(p) else 1, p))
    return out


def install_from_dir(adb: Adb, d: str) -> None:
    if not os.path.isdir(d):
        print(f"no APK directory {d}; create it and drop the APKs there, or use --from-phone")
        return
    entries = sorted(os.listdir(d))
    apks = [e for e in entries if e.lower().endswith(".apk")]
    bundles = [e for e in entries if e.lower().endswith((".xapk", ".apks", ".zip"))]
    subdirs = [e for e in entries if os.path.isdir(os.path.join(d, e))]
    if not (apks or bundles or subdirs):
        print(f"{d} is empty. Download pages: python3 {sys.argv[0]} --list")
        return
    for e in apks:
        print(f"== {e}")
        install_files(adb, [os.path.join(d, e)])
    for e in bundles:
        print(f"== {e} (bundle)")
        with tempfile.TemporaryDirectory() as tmp:
            files = bundle_members(os.path.join(d, e), tmp)
            if files:
                install_files(adb, files)
            else:
                print("   !! no .apk inside")
    for e in subdirs:                      # apks/<pkg>/base.apk + split_*.apk (layout used by --from-phone)
        files = sorted(os.path.join(d, e, f) for f in os.listdir(os.path.join(d, e)) if f.endswith(".apk"))
        if files:
            files.sort(key=lambda p: (0 if os.path.basename(p) == "base.apk" else 1, p))
            print(f"== {e}/ ({len(files)} apk)")
            install_files(adb, files)


def pull_from_phone(phone: Adb, pkgs: list[str], d: str) -> list[str]:
    """Pull base + split APKs of `pkgs` from the phone into d/<pkg>/. Returns the pulled packages."""
    have = installed_packages(phone)
    got = []
    for pkg in pkgs:
        if pkg not in have:
            print(f"-- {display_name(pkg)} ({pkg}): not on the phone")
            continue
        paths = [l.strip()[len("package:"):] for l in phone.shell(f"pm path {pkg}").splitlines()
                 if l.strip().startswith("package:")]
        if not paths:
            continue
        dest = os.path.join(d, pkg)
        os.makedirs(dest, exist_ok=True)
        print(f"== {display_name(pkg)} ({pkg}): {len(paths)} apk")
        for p in paths:
            local = os.path.join(dest, os.path.basename(p))
            if not os.path.exists(local):
                phone.run("pull", p, local, timeout=900)
        got.append(pkg)
    return got


def show_list(adb: Adb | None) -> None:
    have = installed_packages(adb) if adb else set()
    w = max(len(p) for p in CATALOG)
    for pkg, (name, cat, url) in CATALOG.items():
        mark = "installed" if pkg in have else "-"
        print(f"{name:<7s} {pkg:<{w}s} {cat:<9s} {mark:<9s} {url}")
    if adb:
        print(f"\n{len(have & set(CATALOG))}/{len(CATALOG)} catalogue apps installed; emulator ABI {emulator_abi(adb)}")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pkgs", nargs="*", help="restrict --from-phone to these packages (default: whole catalogue)")
    ap.add_argument("--dir", default=DEFAULT_DIR, help="APK directory (default apks/)")
    ap.add_argument("--from-phone", metavar="SERIAL", help="adb serial of a phone to pull the APKs from")
    ap.add_argument("--serial", "-s", help="adb serial of the emulator (default: $ANDROID_SERIAL / the only device)")
    ap.add_argument("--list", action="store_true", help="print the catalogue with download pages and exit")
    args = ap.parse_args(argv)
    try:
        adb = Adb(args.serial)
        adb.shell("echo")
    except AdbError as e:
        if args.list:
            show_list(None)
            return 0
        print(f"emulator not reachable: {e}")
        return 1
    if args.list:
        show_list(adb)
        return 0
    abi = emulator_abi(adb)
    if not abi.startswith("arm64"):
        print(f"!! emulator ABI is {abi}: most mainland-China apps ship arm64/armeabi native code only and will "
              f"fail with INSTALL_FAILED_NO_MATCHING_ABIS or run through ARM translation (very slow). "
              f"Use the arm64-v8a system image (scripts/setup_avd.sh on Apple Silicon).")
    if args.from_phone:
        phone = Adb(args.from_phone)
        pkgs = args.pkgs or list(CATALOG)
        pulled = pull_from_phone(phone, pkgs, args.dir)
        print(f"pulled {len(pulled)} packages into {args.dir}")
    install_from_dir(adb, args.dir)
    have = installed_packages(adb)
    missing = [p for p in CATALOG if p not in have]
    print(f"\n{len(set(CATALOG) & have)}/{len(CATALOG)} catalogue apps installed.")
    if missing:
        print("not installed (official download pages):")
        for p in missing:
            print(f"   {display_name(p):<7s} {p:<32s} {CATALOG[p][2]}")
    print("\nnext: scripts/first_run_cn_apps.sh   (accept privacy dialogs / log in once, by hand)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
