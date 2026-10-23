#!/usr/bin/env bash
# Download the Ubuntu .deb packages of the experiment's desktop apps ON THE MAC (host), so the
# virtual machine only has to install them (scripts/ubuntu/install_cn_apps.sh DIR).  Doing it on
# the host is faster (the Mac's network / proxy, resumable), keeps a copy for re-installs, and
# the resolver logic lives in one place.
#
#     scripts/mac/download_cn_apps.sh --open --scp ubuntu@<vm-ip> --install   # ONE SHOT: download, copy into the VM, install there
#     scripts/mac/download_cn_apps.sh --open --install        # same, run INSIDE the VM (no --scp; installs from the directory)
#     scripts/mac/download_cn_apps.sh                        # arch follows the Mac: Apple Silicon -> arm64, Intel -> amd64
#     scripts/mac/download_cn_apps.sh --arch amd64           # for an x86_64 VM (also on Apple Silicon with emulation)
#     scripts/mac/download_cn_apps.sh --only wechat,qq,wps   # subset; --skip a,b to exclude
#     scripts/mac/download_cn_apps.sh --list                 # where each package comes from, nothing downloaded
#     scripts/mac/download_cn_apps.sh --dry-run              # resolve the URLs only (nothing downloaded or deleted)
#     scripts/mac/download_cn_apps.sh --open                 # open the vendor page in the browser for apps that cannot be resolved
#     scripts/mac/download_cn_apps.sh --scp ubuntu@192.168.64.5   # afterwards copy the directory into the VM (~/Downloads/cf_debs)
#     scripts/mac/download_cn_apps.sh --refresh              # re-download packages that are already present (new versions)
#     scripts/mac/download_cn_apps.sh --keep-foreign         # do not delete debs of the other architecture / broken files
#     scripts/mac/download_cn_apps.sh --vm-repo ~/src/context_freeze   # where the repo is checked out in the VM (for --scp --install)
#     PROXY=http://127.0.0.1:7897 scripts/mac/download_cn_apps.sh  # through Clash etc. (GitHub releases are slow without it)
#     URL_tencent_video=https://... scripts/mac/download_cn_apps.sh   # force a URL for one app (any catalogue id)
#
# Output: ~/Downloads/cf_debs/<app>_<arch>.deb (+ SOURCES.txt with url / size / sha256).  Re-runs are
# cheap: a package that is already in the directory (the script's <app>_<arch>.deb OR a hand-saved
# vendor file such as QQ_3.2.19_arm64.deb / wps-office_12.1_amd64.deb) is not downloaded again and its
# app is not prompted for; --refresh forces new downloads.  Before downloading, files that cannot run
# on the target architecture are deleted: debs built for the other arch (x86_64 debs on an arm64 VM and
# vice versa), *.bad (servers that returned HTML) and files that are not debs at all (--keep-foreign
# disables; .part files of interrupted downloads are kept and resumed).  Then in the VM:
#     sudo scripts/ubuntu/install_cn_apps.sh ~/Downloads/cf_debs      (or let --install do it)
#
# How each package is found (vendors rarely publish stable links; every resolver has a fallback to
# "open the page in the browser"):
#     wechat         fixed universal URL (dldir1v6.qq.com, arm64 / x86_64)
#     qq             im.qq.com's linuxConfig.js (arm64 / amd64)
#     dingtalk       dingtalk.com's rotating redirect  /win/d/qd=linux_<arch>
#     wps            linux.wps.cn (amd64) / 365.wps.cn bundles (arm64)
#     baidunetdisk   pan.baidu.com cmsdata API (amd64 only; arm64 exists only in UOS / Kylin stores)
#     bilibili       GitHub releases API msojocs/bilibili-linux (arm64 / amd64)
#     tencent_video  v.qq.com download page + bundles; no stable public link -> browser fallback
#     chrome / edge  Google direct URL / packages.microsoft.com pool (amd64 only)
#     zwcad, photosir   registration required -> browser
#     firefox, chromium   not downloaded: install the deb versions with apt inside the VM (manual §4.1)
# Needs only bash 3.2 + curl (+ shasum), i.e. a stock macOS.  Also runs unchanged inside the Ubuntu
# VM (arch follows the VM, --open uses xdg-open, --install runs install_cn_apps.sh on the directory).
set -uo pipefail

ARCH=""
OUT="${OUT:-$HOME/Downloads/cf_debs}"
ONLY=""; SKIP=""; LIST=0; DRY=0; OPEN=0; SCP=""; INSTALL=0; REFRESH=0; PRUNE=1; VMREPO='~/context_freeze'
UA="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
ALL_APPS="wechat qq dingtalk wps baidunetdisk tencent_video bilibili chrome edge zwcad photosir"
HERE="$(cd "$(dirname "$0")" && pwd)"

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
    --install) INSTALL=1 ;;
    --refresh) REFRESH=1 ;;
    --keep-foreign) PRUNE=0 ;;
    --vm-repo) VMREPO="$2"; shift ;;
    -h|--help) sed -n '2,44p' "$0"; exit 0 ;;
    *) echo "unknown option $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done

if [ -z "$ARCH" ]; then
  case "$(uname -m)" in arm64|aarch64) ARCH=arm64 ;; *) ARCH=amd64 ;; esac
fi
case "$ARCH" in
  arm64|aarch64) ARCH=arm64; ARCH_RX='(arm64|aarch64)'; OTHER_RX='(amd64|x86_64)' ;;
  amd64|x86_64) ARCH=amd64; ARCH_RX='(amd64|x86_64)'; OTHER_RX='(arm64|aarch64)' ;;
  *) echo "--arch must be arm64 or amd64" >&2; exit 2 ;;
esac
case "$OUT" in /*) ;; *) OUT="$PWD/$OUT" ;; esac
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
browse() {                    # macOS `open`, or xdg-open when run inside the VM
  if command -v open >/dev/null 2>&1; then open "$1"
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$1" >/dev/null 2>&1 &
  else note "open this page in a browser: $1"; fi
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

# ---------------------------------------------------------------- what is already in the directory
is_deb() { [ "$(head -c 7 "$1" 2>/dev/null)" = "!<arch>" ]; }
sha() { if command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | awk '{print $1}'; else sha256sum "$1" | awk '{print $1}'; fi; }
mb() { echo "$(( $(wc -c < "$1") / 1048576 )) MB"; }
vendor_rx() {                 # file names the vendors' own downloads use, so hand-saved debs count as "have"
  case "$1" in
    wechat) echo 'wechat' ;;            qq) echo '^qq[_-]|linuxqq' ;;      dingtalk) echo 'dingtalk' ;;
    wps) echo 'wps' ;;                  baidunetdisk) echo 'baidunetdisk' ;;
    bilibili) echo 'bilibili' ;;        chrome) echo 'google-chrome|^chrome_' ;;
    edge) echo 'microsoft-edge|^edge_' ;;  tencent_video) echo 'tenvideo|tencent' ;;
    zwcad) echo 'zwcad' ;;              photosir) echo 'photosir' ;;
    *) echo "^$1_" ;;
  esac
}
deb_control_arch() {          # Architecture: field of a .deb; dpkg-deb in the VM, else parse the ar members by hand (stock macOS)
  local f="$1" off=8 hdr name size n=0 tmp
  if command -v dpkg-deb >/dev/null 2>&1; then dpkg-deb -f "$f" Architecture 2>/dev/null; return; fi
  while [ $n -lt 3 ]; do
    n=$((n + 1))
    hdr="$(tail -c +$((off + 1)) "$f" 2>/dev/null | head -c 60)"
    [ ${#hdr} -ge 59 ] || return 0        # $(...) drops the header's trailing newline
    name="${hdr:0:16}"; size="$(printf '%s' "${hdr:48:10}" | tr -d ' ')"
    case "$size" in ''|*[!0-9]*) return 0 ;; esac
    case "$name" in
      control.tar*)
        tmp="$(mktemp "${TMPDIR:-/tmp}/cf_control.XXXXXX")" || return 0
        tail -c +$((off + 61)) "$f" | head -c "$size" > "$tmp"
        # bsdtar / GNU tar detect the compression of a file by themselves; a pipe needs the explicit decompressor
        { tar -xOf "$tmp" ./control control 2>/dev/null \
          || case "$name" in *.gz*) gzip -dc "$tmp" ;; *.xz*) xz -dc "$tmp" ;; *.zst*) zstd -dc "$tmp" ;; esac 2>/dev/null \
             | tar -xOf - ./control control 2>/dev/null; } | awk '/^Architecture:/{print $2; exit}'
        rm -f "$tmp"; return 0 ;;
    esac
    off=$((off + 60 + size + (size % 2)))
  done
}
deb_arch() {                  # arm64 / amd64 / all / "" (unknown): control file first, then the file name
  local f="$1" a
  a="$(deb_control_arch "$f")"
  case "$a" in
    arm64|aarch64) echo arm64; return ;; amd64|x86_64|x86-64) echo amd64; return ;; all) echo all; return ;;
  esac
  case "$(basename "$f" | tr 'A-Z' 'a-z')" in
    *arm64*|*aarch64*) echo arm64 ;; *amd64*|*x86_64*|*x86-64*) echo amd64 ;; *) echo "" ;;
  esac
}
have_deb() {                  # prints the deb already present for app $1 (ours or vendor-named, usable on $ARCH)
  local a="$1" f rx
  f="$OUT/${a}_$ARCH.deb"
  if [ -f "$f" ] && is_deb "$f"; then echo "$f"; return 0; fi
  rx="$(vendor_rx "$a")"
  for f in "$OUT"/*.deb; do
    [ -f "$f" ] || continue
    basename "$f" | tr 'A-Z' 'a-z' | grep -qE "$rx" || continue
    is_deb "$f" || continue
    case "$(deb_arch "$f")" in all|"$ARCH"|"") echo "$f"; return 0 ;; esac
  done
  return 1
}
app_of() {                    # catalogue id a file name belongs to (ours or vendor-named), else nothing
  local b a
  b="$(printf '%s' "$1" | tr 'A-Z' 'a-z')"
  for a in $ALL_APPS; do
    case "$b" in "${a}_"*) echo "$a"; return 0 ;; esac
    printf '%s' "$b" | grep -qE "$(vendor_rx "$a")" && { echo "$a"; return 0; }
  done
  return 1
}
forget() {                    # drop app $1's SOURCES.txt line
  [ -n "$1" ] && [ -f "$OUT/SOURCES.txt" ] || return 0
  grep -v "^$1 " "$OUT/SOURCES.txt" > "$OUT/SOURCES.txt.new"; mv -f "$OUT/SOURCES.txt.new" "$OUT/SOURCES.txt"
}
prune() {                     # delete what cannot be installed on an $ARCH VM; never touches .part (resumable downloads)
  local f b a n=0
  for f in "$OUT"/*.deb "$OUT"/*.bad; do
    [ -f "$f" ] || continue
    b="$(basename "$f")"
    case "$f" in
      *.bad) note "rm $b  (server returned something that is not a deb)" ;;
      *) if ! is_deb "$f"; then note "rm $b  (not a .deb: $(head -c 40 "$f" | tr -d '\r\n' | cut -c1-40)...)"
         else
           a="$(deb_arch "$f")"
           case "$a" in
             "$ARCH"|all|"") continue ;;
             *) note "rm $b  (built for $a, the VM is $ARCH)" ;;
           esac
         fi ;;
    esac
    rm -f "$f"; n=$((n + 1))
    forget "$(app_of "$b" || true)"
  done
  [ $n -gt 0 ] && note "removed $n file(s); --keep-foreign keeps them, --out DIR keeps per-arch directories apart"
  return 0
}

# ---------------------------------------------------------------- download
download() {                  # $1 app  $2 url -> $OUT/<app>_<arch>.deb
  local a="$1" u="$2" f="$OUT/${a}_$ARCH.deb" bare="${2%%\?*}"
  note "downloading $(basename "$bare")"
  if ! curl -fL -# -m 3600 --retry 3 --retry-delay 3 -C - -A "$UA" -o "$f.part" "$u"; then
    # some CDNs refuse ranges: start over once without -C
    curl -fL -# -m 3600 --retry 3 -A "$UA" -o "$f.part" "$u" || {
      rm -f "$f.part"; note "!! download failed: $u"
      case "$u" in *qqdl.gtimg.cn*|*wpscdn.cn*|*baidu.com*) note "   (a 403 from this CDN usually means a non-mainland IP: retry without the global proxy, or add the host to Clash's DIRECT rules)" ;; esac
      return 1; }
  fi
  if ! is_deb "$f.part"; then
    note "!! $(basename "$bare") is not a .deb (got $(head -c 60 "$f.part" | tr -d '\r\n' | cut -c1-60)...); kept as $f.bad"
    mv -f "$f.part" "$f.bad"; return 1
  fi
  mv -f "$f.part" "$f"
  { grep -v "^$a " "$OUT/SOURCES.txt" 2>/dev/null; printf '%s  %s  %s bytes  sha256 %s  %s\n' "$a" "$bare" "$(wc -c < "$f" | tr -d ' ')" "$(sha "$f")" "$(date +%F)"; } > "$OUT/SOURCES.txt.new"
  mv -f "$OUT/SOURCES.txt.new" "$OUT/SOURCES.txt"
  note "ok  $(basename "$f")  $(mb "$f")"
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
if [ "$DRY" != 1 ] && [ "$PRUNE" = 1 ]; then prune; fi
got=""; had=""; manual=""; failed=""
for a in $ALL_APPS; do
  want "$a" || continue
  say "-- $a"
  if [ "$REFRESH" != 1 ] && f="$(have_deb "$a")"; then
    note "already have $(basename "$f") ($(mb "$f")); --refresh to download again"
    had="$had $a"; continue
  fi
  url="$(resolve "$a" 2>/dev/null | head -1 || true)"
  if [ -z "$url" ]; then
    why_manual "$a"
    case "$a" in
      baidunetdisk|chrome|edge) [ "$ARCH" = arm64 ] && { note "skipped (no $ARCH build)"; continue; } ;;
    esac
    note "manual: $(page_of "$a")   -> save the deb into $OUT (vendor file name is fine, or ${a}_$ARCH.deb)"
    manual="$manual $a"
    [ "$OPEN" = 1 ] && [ "$DRY" != 1 ] && browse "$(page_of "$a")"
    continue
  fi
  note "url: $url"
  if [ "$DRY" = 1 ]; then got="$got $a"; continue; fi
  if download "$a" "$url"; then got="$got $a"; else failed="$failed $a"; fi
done

say
say "== done: downloaded:${got:- -} | already had:${had:- -} | by hand:${manual:- -} | failed:${failed:- -}"
[ "$DRY" = 1 ] && exit 0
if [ -n "$manual" ]; then
  say "   put the hand-downloaded debs into $OUT and re-run the same command: they are recognised and not asked for again"
fi
in_vm=0; [ "$(uname -s)" = Linux ] && command -v apt-get >/dev/null 2>&1 && in_vm=1
if [ -n "$SCP" ]; then
  say "== copying $OUT -> $SCP:~/Downloads/cf_debs"
  copied=0
  if command -v rsync >/dev/null 2>&1 && ssh "$SCP" 'command -v rsync >/dev/null && mkdir -p ~/Downloads/cf_debs'; then
    rsync -av --include='*.deb' --include='SOURCES.txt' --exclude='*' "$OUT"/ "$SCP:~/Downloads/cf_debs/" && copied=1
  fi
  if [ $copied = 0 ]; then
    ssh "$SCP" 'mkdir -p ~/Downloads/cf_debs' && scp "$OUT"/*.deb "$SCP:~/Downloads/cf_debs/" && copied=1
    [ -f "$OUT/SOURCES.txt" ] && scp "$OUT/SOURCES.txt" "$SCP:~/Downloads/cf_debs/" >/dev/null
  fi
  if [ $copied = 0 ]; then say "!! copy failed"; exit 1; fi
  if [ "$INSTALL" = 1 ]; then
    say "== installing in the VM: sudo $VMREPO/scripts/ubuntu/install_cn_apps.sh ~/Downloads/cf_debs"
    ssh -t "$SCP" "sudo $VMREPO/scripts/ubuntu/install_cn_apps.sh ~/Downloads/cf_debs" \
      || { say "!! install failed (is the repo at $VMREPO in the VM? --vm-repo PATH); by hand:  sudo scripts/ubuntu/install_cn_apps.sh ~/Downloads/cf_debs"; exit 1; }
  else
    say "   in the VM:  sudo scripts/ubuntu/install_cn_apps.sh ~/Downloads/cf_debs   (or add --install to this command)"
  fi
elif [ "$INSTALL" = 1 ] && [ $in_vm = 1 ]; then
  say "== installing: sudo $HERE/../ubuntu/install_cn_apps.sh $OUT"
  sudo "$HERE/../ubuntu/install_cn_apps.sh" "$OUT" || { say "!! install failed"; exit 1; }
elif [ "$INSTALL" = 1 ]; then
  say "!! --install on the Mac needs --scp user@vm (the debs are installed inside the VM)"
else
  say "   next: add --scp ubuntu@<vm-ip> --install (copies into the VM and installs there), or inside the VM:"
  say "         sudo scripts/ubuntu/install_cn_apps.sh ~/Downloads/cf_debs"
fi
[ -z "$failed" ]
