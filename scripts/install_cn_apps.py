#!/usr/bin/env python3
"""Install the mainland-China app set (cf.scenarios.CATALOG) on the emulator.

The apps are not on Google Play, so the APKs come from one of three places:

  1. --download: fetched automatically from Chinese app stores (no phone needed). Sources, in order:
       * 腾讯应用宝 (upage.html5.qq.com/wechat-apkinfo): official metadata with md5, but for several
         apps it serves the 32-bit (armeabi-v7a) build, which the arm64 emulator image (64-bit only
         since API 31) refuses with INSTALL_FAILED_NO_MATCHING_ABIS;
       * 酷安 (api.coolapk.com): resolves to the 64-bit channel of the same CDN for those apps;
       * a few stable official links (DIRECT_URLS).
     Before downloading, the ZIP central directory of the remote APK is read with a Range request
     and its lib/<abi>/ folders are compared with the emulator's ro.product.cpu.abilist, so only
     an installable build is downloaded. Downloads resume after an interruption.

  2. a local directory (default apks/): *.apk, or *.xapk / *.apks / *.zip bundles with split APKs
     (base.apk + split_config.*.apk), downloaded by hand from the official pages listed by --list;

  3. a phone you own, connected over adb next to the emulator (--from-phone <serial>): the
     installed APKs (base + splits) of every catalogue package are pulled with `pm path` and
     installed on the emulator. Phones are arm64 like the emulator, so the native libraries match.

    python3 scripts/install_cn_apps.py --download --config configs/cn_apps.json   # the config's 14 apps, ~3.2 GB
    python3 scripts/install_cn_apps.py --download com.tencent.mm com.sina.weibo    # just these
    python3 scripts/install_cn_apps.py --download                                  # whole 30-app catalogue (~6 GB)
    python3 scripts/install_cn_apps.py --list                    # catalogue + download pages + installed?
    python3 scripts/install_cn_apps.py                           # install everything found in apks/
    python3 scripts/install_cn_apps.py --dir ~/Downloads/apks    # another directory
    python3 scripts/install_cn_apps.py --from-phone 1234ABCD --config configs/cn_apps.json
    python3 scripts/install_cn_apps.py --from-phone 1234ABCD com.tencent.mm com.sina.weibo

With two devices attached (phone + emulator) adb needs to know which one is the target:
export ANDROID_SERIAL=emulator-5554 (or pass --serial). Downloaded / pulled APKs are kept in
apks/<pkg>/ so a fresh AVD can be provisioned later offline (plain `python3 scripts/install_cn_apps.py`).

Afterwards run scripts/first_run_cn_apps.sh once: these apps show a privacy agreement and often a
login screen on first start, which must be dealt with by hand before the experiments.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from cf.adb import Adb, AdbError  # noqa: E402
from cf.scenarios import CATALOG, display_name  # noqa: E402

DEFAULT_DIR = os.path.join(os.path.dirname(__file__), "..", "apks")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"

# Official static links that have stayed put for years; used after the stores (arm64 builds).
DIRECT_URLS = {
    "tv.danmaku.bili": "https://dl.hdslb.com/mobile/latest/android64/iBiliPlayer-bili.apk",
    "com.eg.android.AlipayGphone": "https://t.alipayobjects.com/L1/71/100/and/alipay_wap_main.apk",
}


# ----------------------------------------------------------------------------- automatic download

def _http(url: str, *, method: str = "GET", headers: dict | None = None, data: bytes | None = None,
          timeout: float = 60, follow: bool = True):
    req = urllib.request.Request(url, data=data, method=method, headers={"User-Agent": UA, **(headers or {})})
    if follow:
        return urllib.request.urlopen(req, timeout=timeout)

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    return urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout)


def _redirect_target(url: str, headers: dict | None = None) -> str | None:
    try:
        _http(url, headers=headers, timeout=30, follow=False)
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            return e.headers.get("Location")
    return None


def src_yyb(pkg: str) -> list[dict]:
    """腾讯应用宝: JSON with url, version, size and md5 of the build the store serves."""
    body = json.dumps({"packagename": pkg}).encode()
    with _http("https://upage.html5.qq.com/wechat-apkinfo", data=body,
               headers={"Content-Type": "application/json"}, timeout=30) as r:
        rec = json.load(r).get("app_detail_records", {}).get(pkg)
    if not rec or not rec.get("apk_all_data", {}).get("url"):
        return []
    a = rec["apk_all_data"]
    return [{"source": "应用宝", "url": a["url"], "version": a.get("version_name"),
             "size": int(a.get("size_byte") or 0) or None, "md5": (a.get("apk_md5") or "").lower() or None}]


def src_coolapk(pkg: str) -> list[dict]:
    """酷安: /v6/apk/download redirects (via dl.coolapk.com) to the 64-bit build of the app."""
    def md5(s: str) -> str:
        return hashlib.md5(s.encode()).hexdigest()
    dev, t = md5(uuid.uuid4().hex), int(time.time())
    raw = f"token://com.coolapk.market/c67ef5943784d09750dcfbb31020f0ab?{md5(str(t))}${dev}&com.coolapk.market"
    token = md5(base64.b64encode(raw.encode()).decode()) + dev + "0x" + format(t, "x")
    hdr = {"User-Agent": "Dalvik/2.1.0 (Linux; U; Android 9; MI 8 SE MIUI/9.5.9) (#Build; Xiaomi; MI 8 SE; "
                         "PKQ1.181121.001; 9) +CoolMarket/9.2.2-1905301",
           "X-Requested-With": "XMLHttpRequest", "X-Sdk-Int": "28", "X-Sdk-Locale": "zh-CN",
           "X-App-Id": "com.coolapk.market", "X-App-Token": token, "X-App-Version": "9.2.2",
           "X-App-Code": "1905301", "X-Api-Version": "9"}
    loc = _redirect_target(f"https://api.coolapk.com/v6/apk/download?pn={pkg}&aid=0", hdr)
    if not loc:
        return []
    final = _redirect_target(loc) or loc
    return [{"source": "酷安", "url": final, "version": None, "size": None, "md5": None}]


def src_direct(pkg: str) -> list[dict]:
    url = DIRECT_URLS.get(pkg)
    return [{"source": "官网", "url": url, "version": None, "size": None, "md5": None}] if url else []


SOURCES = (src_yyb, src_coolapk, src_direct)


def remote_apk_abis(url: str) -> tuple[list[str], int]:
    """lib/<abi>/ folders of a remote APK, read from its ZIP central directory with two Range requests
    (a few hundred kB instead of the whole file). Returns ([], size) for an APK without native code."""
    with _http(url, method="HEAD", timeout=30) as r:
        size = int(r.headers["Content-Length"])

    def rng(a: int, b: int) -> bytes:
        with _http(url, headers={"Range": f"bytes={a}-{b}"}, timeout=60) as r:
            return r.read()
    tail = rng(max(0, size - 65536), size - 1)
    i = tail.rfind(b"PK\x05\x06")
    if i < 0:
        raise ValueError("not a ZIP file (no end-of-central-directory record)")
    cd_size, cd_off = struct.unpack("<II", tail[i + 12:i + 20])
    if cd_off == 0xFFFFFFFF:
        raise ValueError("zip64 APK, ABI probe not supported")
    cd = rng(cd_off, cd_off + cd_size - 1)
    names, p = [], 0
    while p + 46 <= len(cd) and cd[p:p + 4] == b"PK\x01\x02":
        n, m, k = struct.unpack("<HHH", cd[p + 28:p + 34])
        names.append(cd[p + 46:p + 46 + n].decode("utf-8", "replace"))
        p += 46 + n + m + k
    if "AndroidManifest.xml" not in names:
        raise ValueError("ZIP without AndroidManifest.xml (not an APK)")
    return sorted({x.split("/")[1] for x in names if x.startswith("lib/") and x.count("/") >= 2}), size


def download(url: str, dest: str, size: int | None, md5: str | None) -> bool:
    """Download with resume (Range) into dest.part, then verify size / md5 / ZIP and rename."""
    part = dest + ".part"
    for attempt in range(1, 4):
        have = os.path.getsize(part) if os.path.exists(part) else 0
        if size and have >= size:
            break
        try:
            hdr = {"Range": f"bytes={have}-"} if have else {}
            with _http(url, headers=hdr, timeout=120) as r, open(part, "ab" if have else "wb") as f:
                if have and r.status != 206:      # server ignored the Range header: start over
                    f.seek(0); f.truncate(); have = 0
                total = size or (have + int(r.headers.get("Content-Length") or 0)) or None
                t0, last = time.time(), 0.0
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk); have += len(chunk)
                    if time.time() - last > 0.5:
                        pct = f"{100 * have / total:3.0f}%" if total else "    "
                        rate = (have / 1e6) / max(time.time() - t0, 1e-3)
                        print(f"\r   {pct} {have / 1e6:6.0f} MB  {rate:5.1f} MB/s   ", end="", flush=True)
                        last = time.time()
            print("\r" + " " * 50 + "\r", end="")
            break
        except (urllib.error.URLError, OSError, ConnectionError) as e:
            print(f"\n   !! download interrupted ({e}); retry {attempt}/3")
            time.sleep(3 * attempt)
    else:
        return False
    if not os.path.exists(part):
        return False
    if size and os.path.getsize(part) != size:
        print(f"   !! size mismatch: {os.path.getsize(part)} vs {size} bytes"); os.remove(part); return False
    if md5:
        h = hashlib.md5()
        with open(part, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 22), b""):
                h.update(chunk)
        if h.hexdigest() != md5:
            print(f"   !! md5 mismatch ({h.hexdigest()} vs {md5})"); os.remove(part); return False
    try:
        with zipfile.ZipFile(part) as z:
            if "AndroidManifest.xml" not in z.namelist():
                raise zipfile.BadZipFile("no AndroidManifest.xml")
    except zipfile.BadZipFile as e:
        print(f"   !! not a valid APK: {e}"); os.remove(part); return False
    os.replace(part, dest)
    return True


def auto_download(adb: Adb, pkgs: list[str], d: str, dry_run: bool = False) -> list[str]:
    """Download an installable build of every package in pkgs into d/<pkg>/. Returns the packages
    that now have an APK on disk."""
    abilist = [a for a in adb.shell("getprop ro.product.cpu.abilist").strip().split(",") if a]
    print(f"emulator ABIs: {', '.join(abilist)}   (APKs whose native code is only for other ABIs are skipped)")
    got = []
    for pkg in pkgs:
        dest_dir = os.path.join(d, pkg)
        existing = [f for f in os.listdir(dest_dir)] if os.path.isdir(dest_dir) else []
        if any(f.endswith(".apk") for f in existing):
            print(f"== {display_name(pkg)} ({pkg}): already in {dest_dir}")
            got.append(pkg)
            continue
        print(f"== {display_name(pkg)} ({pkg})")
        ok = False
        for src in SOURCES:
            try:
                cands = src(pkg)
            except Exception as e:                       # one store down must not stop the others
                print(f"   {src.__name__[4:]}: lookup failed ({e})")
                continue
            for c in cands:
                for attempt in (1, 2, 3):                # CDN handshakes time out now and then
                    try:
                        abis, size = remote_apk_abis(c["url"])
                        break
                    except Exception as e:
                        if attempt == 3:
                            print(f"   {c['source']}: cannot inspect APK ({e})")
                        else:
                            time.sleep(2 * attempt)
                else:
                    continue
                if abis and not set(abis) & set(abilist):
                    print(f"   {c['source']}: v{c['version'] or '?'} {size / 1e6:.0f} MB, native libs only for "
                          f"{'/'.join(abis)} - not installable here, trying the next source")
                    continue
                print(f"   {c['source']}: v{c['version'] or '?'} {size / 1e6:.0f} MB, libs {'/'.join(abis) or 'none'}")
                if dry_run:
                    got.append(pkg); ok = True
                    break
                os.makedirs(dest_dir, exist_ok=True)
                ver = f"-{c['version']}" if c["version"] else ""
                dest = os.path.join(dest_dir, f"{pkg}{ver}.apk")
                if download(c["url"], dest, c["size"] or size, c["md5"]):
                    got.append(pkg); ok = True
                    break
            if ok:
                break
        if not ok:
            print(f"   !! no installable build found for {pkg}; download it by hand from {CATALOG[pkg][2]} into {d}/")
    return got


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
        print(f"no APK directory {d}; use --download, or create it and drop the APKs there, or use --from-phone")
        return
    entries = sorted(os.listdir(d))
    apks = [e for e in entries if e.lower().endswith(".apk")]
    bundles = [e for e in entries if e.lower().endswith((".xapk", ".apks", ".zip"))]
    subdirs = [e for e in entries if os.path.isdir(os.path.join(d, e))]
    if not (apks or bundles or subdirs):
        print(f"{d} is empty. Fetch the APKs automatically: python3 {sys.argv[0]} --download --config configs/cn_apps.json"
              f"   (or by hand from the pages listed by --list)")
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
    ap.add_argument("pkgs", nargs="*", help="restrict --download / --from-phone to these packages (default: --config's apps, else the whole catalogue)")
    ap.add_argument("--dir", default=DEFAULT_DIR, help="APK directory (default apks/)")
    ap.add_argument("--download", action="store_true", help="download the APKs from Chinese app stores (应用宝 / 酷安 / official links), then install")
    ap.add_argument("--dry-run", action="store_true", help="with --download: only show which build each source offers, download nothing")
    ap.add_argument("--from-phone", metavar="SERIAL", help="adb serial of a phone to pull the APKs from")
    ap.add_argument("--config", metavar="JSON", help="with --download / --from-phone: only the apps listed in this config")
    ap.add_argument("--serial", "-s", help="adb serial of the emulator (default: $ANDROID_SERIAL / the only device)")
    ap.add_argument("--list", action="store_true", help="print the catalogue with download pages and exit")
    args = ap.parse_args(argv)
    pkgs = args.pkgs or list(CATALOG)
    if args.config and not args.pkgs:
        with open(args.config, encoding="utf-8") as f:
            pkgs = json.load(f)["apps"]
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
    if args.from_phone is not None:
        # `--from-phone "$PHONE"` with an empty PHONE (no phone detected) used to fall through silently
        # to the apks/ directory; fail here and show what adb actually sees instead.
        devices = [l.split() for l in adb.run("devices", "-l").splitlines()[1:] if l.strip()]
        phones = [d for d in devices if not d[0].startswith("emulator-")]
        if not args.from_phone.strip():
            print("!! --from-phone got an empty serial: no phone was detected. adb devices -l:")
            for d in devices:
                print(f"     {d[0]:<24s} {d[1]}" + ("   <- tap 允许/Allow USB debugging on the phone" if d[1] == "unauthorized" else ""))
            if not phones:
                print("   Plug the phone in with USB debugging on (设置 > 关于手机 > 连点版本号 7 次 -> 开发者选项 > USB 调试),"
                      " choose 传输文件/MTP if it only charges, then re-run.")
            print(f"   No phone? Use --download instead: python3 {sys.argv[0]} --download"
                  + (f" --config {args.config}" if args.config else ""))
            return 1
        if args.from_phone not in [d[0] for d in devices if d[1] == "device"]:
            print(f"!! phone {args.from_phone} is not an online adb device. adb devices -l:")
            for d in devices:
                print(f"     {d[0]:<24s} {d[1]}")
            return 1
        phone = Adb(args.from_phone)
        pulled = pull_from_phone(phone, pkgs, args.dir)
        print(f"pulled {len(pulled)} packages into {args.dir}")
    if args.download:
        have = installed_packages(adb)
        todo = [p for p in pkgs if p not in have] if not args.pkgs else pkgs
        skipped = len(pkgs) - len(todo)
        print(f"downloading {len(todo)} apps into {os.path.abspath(args.dir)}"
              + (f" ({skipped} already installed, skipped)" if skipped else ""))
        got = auto_download(adb, todo, args.dir, dry_run=args.dry_run)
        if args.dry_run:
            print(f"{len(got)}/{len(todo)} apps have an installable build (dry run, nothing downloaded)")
            return 0
        print(f"{len(got)}/{len(todo)} APKs on disk")
    install_from_dir(adb, args.dir)
    have = installed_packages(adb)
    df = adb.shell("df -h /data | tail -1").split()
    if len(df) >= 4:
        print(f"\n/data: {df[2]} used, {df[3]} free (these apps take 0.3-1 GB each incl. data; "
              f"AVD data partition is 8 GB by default)")
    print(f"\n{len(set(CATALOG) & have)}/{len(CATALOG)} catalogue apps installed.")
    missing_cfg = [p for p in pkgs if p not in have]
    if missing_cfg:
        print(f"not installed from the requested set ({len(missing_cfg)}):")
        for p in missing_cfg:
            print(f"   {display_name(p):<7s} {p:<32s} {CATALOG.get(p, ('', '', ''))[2]}")
        if not args.download:
            print(f"   -> python3 {sys.argv[0]} --download" + (f" --config {args.config}" if args.config else ""))
    print("\nnext: scripts/first_run_cn_apps.sh   (accept privacy dialogs / log in once, by hand)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
