#!/usr/bin/env bash
# Download the Ubuntu .deb packages of the experiment's desktop apps ON THE MAC (host), so the
# virtual machine only has to install them (scripts/ubuntu/install_cn_apps.sh DIR).  Doing it on
# the host is faster (the Mac's network / proxy, resumable), keeps a copy for re-installs, and
# the resolver logic lives in one place.
#
#     scripts/mac/download_cn_apps.sh                        # arch follows the Mac: Apple Silicon -> arm64, Intel -> amd64
#     scripts/mac/download_cn_apps.sh --arch amd64           # for an x86_64 VM (also on Apple Silicon with emulation)
#     scripts/mac/download_cn_apps.sh --only wechat,qq,wps   # subset; --skip a,b to exclude
#     scripts/mac/download_cn_apps.sh --list                 # where each package comes from, nothing downloaded
#     scripts/mac/download_cn_apps.sh --dry-run              # resolve the URLs only
#     scripts/mac/download_cn_apps.sh --open                 # open the vendor page in the browser for apps that cannot be resolved
#     scripts/mac/download_cn_apps.sh --scp ubuntu@192.168.64.5   # afterwards copy the directory into the VM (~/Downloads/cf_debs)
#     PROXY=http://127.0.0.1:7897 scripts/mac/download_cn_apps.sh  # through Clash etc. (GitHub releases are slow without it)
#     URL_tencent_video=https://... scripts/mac/download_cn_apps.sh   # force a URL for one app (any catalogue id)
#
# Output: ~/Downloads/cf_debs/<app>_<arch>.deb (+ SOURCES.txt with url / size / sha256).  Then in the VM:
#     sudo scripts/ubuntu/install_cn_apps.sh ~/Downloads/cf_debs
#
# How each package is found (vendors rarely publish stable links; every resolver has a fallback to
# "open the page in the browser"):
#     wechat         fixed universal URL (dldir1v6.qq.com, arm64 / x86_64)
#     qq             im.qq.com's linuxQQDownload.js (arm64 / amd64)
#     dingtalk       dingtalk.com's rotating redirect  /win/d/qd=linux_<arch>
#     wps            linux.wps.cn (amd64) / 365.wps.cn bundles (arm64)
#     baidunetdisk   pan.baidu.com cmsdata API (amd64 only; arm64 exists only in UOS / Kylin stores)
#     bilibili       GitHub releases API msojocs/bilibili-linux (arm64 / amd64)
#     tencent_video  v.qq.com download page + bundles; no stable public link -> browser fallback
#     chrome / edge  Google direct URL / packages.microsoft.com pool (amd64 only)
#     zwcad, photosir   registration required -> browser
#     firefox, chromium   not downloaded: install the deb versions with apt inside the VM (manual §4.1)
# Needs only bash 3.2 + curl (+ shasum), i.e. a stock macOS.  Also runs on Linux.
set -uo pipefail

ARCH=""
OUT="${OUT:-$HOME/Downloads/cf_debs}"
ONLY=""; SKIP=""; LIST=0; DRY=0; OPEN=0; SCP=""
UA="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
ALL_APPS="wechat qq dingtalk wps baidunetdisk tencent_video bilibili chrome edge zwcad photosir"

while [ $# -gt 0 ]; do
  case "$1" in
    --arch) ARCH="$2"; shift ;;
    --out) OUT="$2"; shift ;;
    --only) ONLY="$2"; shift ;;
    --skip) SKIP="$2"; shift ;;
    --list) LIST=1 ;;
    --dry-run) DRY=1 ;;
    --open) OPEN=1 ;;
    --scp) SCP="$2"; shift ;;
    -h|--help) sed -n '2,32p' "$0"; exit 0 ;;
    *) echo "unknown option $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done

if [ -z "$ARCH" ]; then
  case "$(uname -m)" in arm64|aarch64) ARCH=arm64 ;; *) ARCH=amd64 ;; esac
fi
case "$ARCH" in
  arm64|aarch64) ARCH=arm64; ARCH_RX='(arm64|aarch64)' ;;
  amd64|x86_64) ARCH=amd64; ARCH_RX='(amd64|x86_64)' ;;
  *) echo "--arch must be arm64 or amd64" >&2; exit 2 ;;
esac
if [ -n "${PROXY:-}" ]; then export http_proxy="$PROXY" https_proxy="$PROXY" all_proxy="$PROXY"; fi

say()  { printf '%s\n' "$*"; }
note() { printf '  %s\n' "$*"; }
want() {                      # is app $1 selected?
  local a="$1"
  if [ -n "$ONLY" ] && ! printf ',%s,' "$ONLY" | grep -q ",$a,"; then return 1; fi
  if [ -n "$SKIP" ] && printf ',%s,' "$SKIP" | grep -q ",$a,"; then return 1; fi
  return 0
}
fetch() { curl -fsSL -m 40 --retry 2 -A "$UA" "$@" 2>/dev/null; }
debs_in() {                   # stdin: html / js -> absolute .deb urls (handles \/ escapes and //host paths)
  sed 's#\\/#/#g' | grep -oE '(https?:)?//[^"'"'"' <>\\,()]+\.deb' | sed 's#^//#https://#' | sort -u
}
js_in() { sed 's#\\/#/#g' | grep -oE '(https?:)?//[^"'"'"' <>\\,()]+\.js' | sed 's#^//#https://#' | sort -u; }
pick_arch() { grep -E "$ARCH_RX" | grep -viE 'loong|mips|riscv' | head -1; }
scrape() {                    # $1 page url: .deb links on the page, else inside its (first 20) script bundles
  local page hit js n=0
  page="$(fetch "$1")" || return 1
  hit="$(printf '%s' "$page" | debs_in | pick_arch)"
  if [ -n "$hit" ]; then printf '%s\n' "$hit"; return 0; fi
  for js in $(printf '%s' "$page" | js_in | grep -viE 'jquery|hm\.baidu|aplus|analytics|beacon|polyfill|vendor-vue|crypto-js'); do
    n=$((n + 1)); [ $n -gt 20 ] && break
    hit="$(fetch "$js" | debs_in | pick_arch)"
    if [ -n "$hit" ]; then printf '%s\n' "$hit"; return 0; fi
  done
  return 1
}
redirect_of() { curl -s -o /dev/null -m 30 -A "$UA" -w '%{redirect_url}' "$1" 2>/dev/null; }
md5str() { if command -v md5 >/dev/null 2>&1; then md5 -q -s "$1"; else printf '%s' "$1" | md5sum | cut -c1-32; fi; }
wps_signed() {                # linux.wps.cn signs its CDN links in the page's downLoad(): ?t=<unix>&k=md5(key+path+t)
  local page u key path t
  page="$(fetch "https://linux.wps.cn/")" || return 1
  u="$(printf '%s' "$page" | debs_in | pick_arch)"; [ -n "$u" ] || return 1
  key="$(printf '%s' "$page" | grep -oE 'secrityKey *= *"[0-9a-f]+"' | grep -oE '[0-9a-f]{32}' | head -1)"
  if [ -z "$key" ]; then echo "$u"; return 0; fi          # page changed: try the bare link
  path="/${u#*://*/}"; t="$(date +%s)"
  echo "$u?t=$t&k=$(md5str "$key$path$t")"
}

# ---------------------------------------------------------------- resolvers: print one URL or nothing
page_of() {
  case "$1" in
    wechat) echo "https://linux.weixin.qq.com/" ;;
    qq) echo "https://im.qq.com/linuxqq/" ;;
    dingtalk) echo "https://page.dingtalk.com/wow/dingtalk/act/download" ;;
    wps) [ "$ARCH" = arm64 ] && echo "https://365.wps.cn/download365" || echo "https://linux.wps.cn/" ;;
    baidunetdisk) echo "https://pan.baidu.com/download#linux" ;;
    tencent_video) echo "https://v.qq.com/biu/download" ;;
    bilibili) echo "https://github.com/msojocs/bilibili-linux/releases" ;;
    chrome) echo "https://www.google.com/chrome/" ;;
    edge) echo "https://www.microsoft.com/edge/download?platform=linux" ;;
    zwcad) echo "https://www.zwsoft.cn/product/zwcad-linux" ;;
    photosir) echo "https://www.photosir.com/" ;;
  esac
}
amd64_only_note() { note "$1 publishes no arm64 build; in an arm64 VM the runner falls back to $2"; }
resolve() {
  local a="$1" v
  eval "v=\${URL_$a:-}"
  if [ -n "$v" ]; then echo "$v"; return 0; fi
  case "$a" in
    wechat)
      [ "$ARCH" = arm64 ] && v=arm64 || v=x86_64
      echo "https://dldir1v6.qq.com/weixin/Universal/Linux/WeChatLinux_$v.deb" ;;
    qq)
      fetch "https://cdn-go.cn/qq-web/im.qq.com_new/latest/rainbow/linuxConfig.js" | debs_in | pick_arch \
        || scrape "https://im.qq.com/linuxqq/index.shtml" ;;
    dingtalk)
      v="$(redirect_of "https://www.dingtalk.com/win/d/qd=linux_$ARCH")"
      case "$v" in *.deb) echo "$v" ;; *) scrape "$(page_of dingtalk)" ;; esac ;;
    wps)
      if [ "$ARCH" = amd64 ]; then wps_signed || scrape "https://365.wps.cn/download365"
      else scrape "https://365.wps.cn/download365"; fi ;;
    baidunetdisk)
      [ "$ARCH" = amd64 ] || return 1
      fetch "https://pan.baidu.com/disk/cmsdata?clienttype=0&web=1&do=client" | debs_in | pick_arch ;;
    tencent_video)
      scrape "https://v.qq.com/biu/download" || scrape "https://v.qq.com/download.html" ;;
    bilibili)
      fetch "https://api.github.com/repos/msojocs/bilibili-linux/releases/latest" \
        | grep -oE '"browser_download_url": *"[^"]+\.deb"' | grep -oE 'https://[^"]+' | pick_arch ;;
    chrome)
      [ "$ARCH" = amd64 ] || return 1
      echo "https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb" ;;
    edge)
      [ "$ARCH" = amd64 ] || return 1
      v="$(fetch "https://packages.microsoft.com/repos/edge/pool/main/m/microsoft-edge-stable/" \
            | grep -oE 'microsoft-edge-stable_[0-9.]+-[0-9]+_amd64\.deb' | sort -u \
            | sed -E 's/^microsoft-edge-stable_([0-9]+)\.([0-9]+)\.([0-9]+)\.([0-9]+)-.*/\1 \2 \3 \4 &/' \
            | sort -k1,1n -k2,2n -k3,3n -k4,4n | tail -1 | awk '{print $5}')"
      [ -n "$v" ] && echo "https://packages.microsoft.com/repos/edge/pool/main/m/microsoft-edge-stable/$v" ;;
    zwcad|photosir) return 1 ;;
    *) return 1 ;;
  esac
}
why_manual() {
  case "$1" in
    tencent_video) note "v.qq.com serves the Linux link from a script-only page; download it by hand from the page"
                   note "(or pass URL_tencent_video=... ; unofficial mirrors exist in the AUR 'tenvideo' PKGBUILD)" ;;
    baidunetdisk) [ "$ARCH" = arm64 ] && amd64_only_note "pan.baidu.com" "baidunetdisk_mobile (Waydroid)" ;;
    chrome) [ "$ARCH" = arm64 ] && amd64_only_note "Chrome" "nothing (use Firefox / Chromium debs)" ;;
    edge) [ "$ARCH" = arm64 ] && amd64_only_note "Edge" "nothing (use Firefox / Chromium debs)" ;;
    zwcad) note "ZWCAD Linux needs a registration / trial form; download the $ARCH deb from the page" ;;
    photosir) note "PhotoSir (悟空图像) download page; pick the Linux $ARCH deb" ;;
  esac
}

# ---------------------------------------------------------------- download
is_deb() { [ "$(head -c 7 "$1" 2>/dev/null)" = "!<arch>" ]; }
sha() { if command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | awk '{print $1}'; else sha256sum "$1" | awk '{print $1}'; fi; }
download() {                  # $1 app  $2 url -> $OUT/<app>_<arch>.deb
  local a="$1" u="$2" f="$OUT/${a}_$ARCH.deb" bare="${2%%\?*}"
  if [ -f "$f" ] && is_deb "$f" && grep -qF "$a  $bare " "$OUT/SOURCES.txt" 2>/dev/null; then
    note "already have $(basename "$f") ($(( $(wc -c < "$f") / 1048576 )) MB)"; return 0
  fi
  note "downloading $(basename "$u")"
  if ! curl -fL -# -m 3600 --retry 3 --retry-delay 3 -C - -A "$UA" -o "$f.part" "$u"; then
    # some CDNs refuse ranges: start over once without -C
    curl -fL -# -m 3600 --retry 3 -A "$UA" -o "$f.part" "$u" || {
      rm -f "$f.part"; note "!! download failed: $u"
      case "$u" in *qqdl.gtimg.cn*|*wpscdn.cn*|*baidu.com*) note "   (a 403 from this CDN usually means a non-mainland IP: retry without the global proxy, or add the host to Clash's DIRECT rules)" ;; esac
      return 1; }
  fi
  if ! is_deb "$f.part"; then
    note "!! $(basename "$u") is not a .deb (got $(head -c 60 "$f.part" | tr -d '\r\n' | cut -c1-60)...); kept as $f.bad"
    mv -f "$f.part" "$f.bad"; return 1
  fi
  mv -f "$f.part" "$f"
  { grep -v "^$a " "$OUT/SOURCES.txt" 2>/dev/null; printf '%s  %s  %s bytes  sha256 %s  %s\n' "$a" "$bare" "$(wc -c < "$f" | tr -d ' ')" "$(sha "$f")" "$(date +%F)"; } > "$OUT/SOURCES.txt.new"
  mv -f "$OUT/SOURCES.txt.new" "$OUT/SOURCES.txt"
  note "ok  $(basename "$f")  $(( $(wc -c < "$f") / 1048576 )) MB"
}

# ---------------------------------------------------------------- main
if [ "$LIST" = 1 ]; then
  say "packages for an $ARCH VM (resolver -> fallback page):"
  for a in $ALL_APPS; do want "$a" || continue; printf '  %-14s %s\n' "$a" "$(page_of "$a")"; done
  say "firefox / chromium: apt inside the VM (Mozilla APT repo deb / ppa:xtradeb/apps); jianying / xiaoyi: mobile builds only (install_mobile_apps.sh)"
  exit 0
fi
[ "$DRY" = 1 ] || mkdir -p "$OUT"
say "== Ubuntu $ARCH packages -> $OUT ${PROXY:+(proxy $PROXY)}"
got=""; manual=""; failed=""
for a in $ALL_APPS; do
  want "$a" || continue
  say "-- $a"
  url="$(resolve "$a" 2>/dev/null | head -1 || true)"
  if [ -z "$url" ]; then
    why_manual "$a"
    case "$a" in
      baidunetdisk|chrome|edge) [ "$ARCH" = arm64 ] && { note "skipped (no $ARCH build)"; continue; } ;;
    esac
    note "manual: $(page_of "$a")   -> save the deb as $OUT/${a}_$ARCH.deb"
    manual="$manual $a"
    [ "$OPEN" = 1 ] && command -v open >/dev/null 2>&1 && open "$(page_of "$a")"
    continue
  fi
  note "url: $url"
  if [ "$DRY" = 1 ]; then got="$got $a"; continue; fi
  if download "$a" "$url"; then got="$got $a"; else failed="$failed $a"; fi
done

say
say "== done: downloaded:${got:- -} | by hand:${manual:- -} | failed:${failed:- -}"
[ "$DRY" = 1 ] && exit 0
if [ -n "$manual" ]; then
  say "   put the hand-downloaded debs into $OUT (any file name ending in .deb is installed)"
fi
if [ -n "$SCP" ]; then
  say "== copying $OUT to $SCP:~/Downloads/cf_debs"
  ssh "$SCP" 'mkdir -p ~/Downloads/cf_debs' && scp -r "$OUT"/*.deb "$OUT"/SOURCES.txt "$SCP:~/Downloads/cf_debs/" \
    && say "   in the VM:  sudo scripts/ubuntu/install_cn_apps.sh ~/Downloads/cf_debs"
else
  say "   next: scp -r $OUT ubuntu@<vm-ip>:~/Downloads/   (or a UTM / Parallels shared folder), then in the VM:"
  say "         sudo scripts/ubuntu/install_cn_apps.sh ~/Downloads/cf_debs"
fi
[ -z "$failed" ]
