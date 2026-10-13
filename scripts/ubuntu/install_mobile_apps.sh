#!/usr/bin/env bash
# Install the mobile (Android) builds of the apps into Waydroid - only for apps whose Ubuntu
# desktop build is missing.  Wraps scripts/install_cn_apps.py (应用宝 / 酷安 downloader, ABI check,
# split-APK support) with the Waydroid adb serial.
#
#   scripts/ubuntu/install_mobile_apps.sh --list                 # desktop -> mobile chain per app, what is installed
#   scripts/ubuntu/install_mobile_apps.sh com.lemon.lv           # download + install 剪映 (手机版)
#   scripts/ubuntu/install_mobile_apps.sh --missing              # every app of the config whose desktop build is absent
#   scripts/ubuntu/install_mobile_apps.sh --from-phone <serial> com.huawei.vassistant com.huawei.hwid   # 小艺 from a Huawei phone
#   scripts/ubuntu/install_mobile_apps.sh --search 中望           # look a package name up in 酷安 (for zwcad / photosir)
#   scripts/ubuntu/install_mobile_apps.sh --dir ~/apks           # install *.apk / *.xapk you downloaded by hand
#
# Serial: $CF_WAYDROID_SERIAL, else the IP from `waydroid status` + :5555, else 192.168.240.112:5555.
set -euo pipefail
cd "$(dirname "$0")/../.."
CONFIG="${CF_CONFIG:-configs/ubuntu_cn_desktop.json}"
serial="${CF_WAYDROID_SERIAL:-}"
if [ -z "$serial" ] && command -v waydroid >/dev/null; then
  ip="$(waydroid status 2>/dev/null | awk -F': *' '/IP address/{print $2}')"
  [ -n "$ip" ] && serial="$ip:5555"
fi
serial="${serial:-192.168.240.112:5555}"
adb connect "$serial" >/dev/null 2>&1 || true

case "${1:-}" in
  --list)
    python3 - "$CONFIG" <<'PY'
import json, sys
from cf.linux import apps as A
from cf.linux.mobile import MobileRuntime
cfg = json.load(open(sys.argv[1]))
cat = A.with_overrides(cfg.get("app_overrides"))
mb = MobileRuntime(cfg.get("mobile")); mb.probe()
print(mb.describe())
print(f"{'app':14s} {'desktop build':14s} {'mobile build (Waydroid)':40s} status")
for id_ in cfg.get("apps", []):
    if id_ not in cat:
        print(f"{id_:14s} unknown"); continue
    a = cat[id_]
    desk = "installed" if A.is_installed(a) else ("none exists" if not a.linux else "not installed")
    mob = next((cat[x] for x in A.substitute_chain(id_, cat)[1:] if cat[x].is_mobile), None)
    if mob is None:
        mstr, st = "- (no mobile entry; --search)", "RUN desktop" if desk == "installed" else "DROPPED"
    else:
        inst = A.is_installed(mob, mobile=mb)
        mstr = f"{mob.package} {'installed' if inst else 'not installed'}"
        st = "RUN desktop" if desk == "installed" else ("RUN mobile" if inst else f"DROPPED -> install_mobile_apps.sh {mob.package}")
    print(f"{id_:14s} {desk:14s} {mstr:40s} {st}")
PY
    exit 0 ;;
  --missing)
    pkgs="$(python3 - "$CONFIG" <<'PY'
import json, sys
from cf.linux import apps as A
cfg = json.load(open(sys.argv[1])); cat = A.with_overrides(cfg.get("app_overrides"))
out = []
for id_ in cfg.get("apps", []):
    a = cat.get(id_)
    if a and not A.is_installed(a):
        for x in A.substitute_chain(id_, cat)[1:]:
            if cat[x].is_mobile and cat[x].package not in out:
                out.append(cat[x].package)
print(" ".join(out))
PY
)"
    [ -z "$pkgs" ] && { echo "every app of $CONFIG has its desktop build installed - nothing to do"; exit 0; }
    echo "== desktop build missing -> installing mobile builds: $pkgs"
    exec python3 scripts/install_cn_apps.py --download -s "$serial" $pkgs ;;
  --search)
    shift
    python3 - "$@" <<'PY'
import json, sys, urllib.parse, urllib.request
q = " ".join(sys.argv[1:]) or sys.exit("usage: --search <name>")
url = "https://api.coolapk.com/v6/search?type=apk&searchValue=" + urllib.parse.quote(q)
req = urllib.request.Request(url, headers={"User-Agent": "Dalvik/2.1.0 (Linux; U; Android 12) +CoolMarket/12.4.2",
                                           "X-App-Id": "com.coolapk.market", "X-App-Version": "12.4.2", "X-Api-Version": "12"})
try:
    data = json.load(urllib.request.urlopen(req, timeout=20)).get("data", [])
except Exception as e:  # noqa: BLE001
    sys.exit(f"coolapk search failed ({e}); look the package name up on the vendor's download page "
             f"(the APK link usually contains it) and add it in app_overrides as <id>_mobile")
for it in data[:15]:
    print(f"{it.get('apkname','?'):40s} {it.get('title','?')}  v{it.get('apkversionname','?')}")
PY
    exit 0 ;;
  --from-phone)
    shift; phone="$1"; shift
    exec python3 scripts/install_cn_apps.py --from-phone "$phone" -s "$serial" "$@" ;;
  --dir)
    shift
    exec python3 scripts/install_cn_apps.py --dir "$1" -s "$serial" ;;
  "" | -h | --help)
    sed -n '2,13p' "$0"; exit 0 ;;
  *)
    exec python3 scripts/install_cn_apps.py --download -s "$serial" "$@" ;;
esac
