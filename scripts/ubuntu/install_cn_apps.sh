#!/usr/bin/env bash
# Install the mainland-China desktop apps from a directory of .deb files.  Get the debs either with
# scripts/mac/download_cn_apps.sh (resolves the vendors' links; --scp / --install hand over to this
# script) or by hand from the pages printed by --list (save them into one directory, vendor file names
# are fine - packages are recognised by the control file, not the name).
#
#     scripts/ubuntu/install_cn_apps.sh --list            # per app: download page, arch, package name
#     sudo scripts/ubuntu/install_cn_apps.sh ~/Downloads/cf_debs   # every deb: arch check -> skip if that version is installed -> apt install ./x.deb
#     sudo scripts/ubuntu/install_cn_apps.sh --firefox [DIR]       # also install the Mozilla APT deb build of Firefox (not the snap) + xclip
#     scripts/ubuntu/install_cn_apps.sh --check           # what the runner will consider installed
#
# After installing, start each app once by hand (scripts/ubuntu/cf_launch.sh <id>), accept the
# agreements, log in, and run `python3 run_linux.py configs/ubuntu_cn_desktop.json --discover` to
# verify the WM_CLASS / command names the catalogue assumes (override in app_overrides if they differ).
set -euo pipefail
cd "$(dirname "$0")/../.."
FIREFOX=0
case "${1:-}" in
  --list)
    python3 - <<'PY'
from cf.linux import apps as A
import platform
arch = platform.machine()
print(f"Ubuntu desktop builds (install these first; this machine is {arch}); '-> mobile' = the app's own Android build "
      "used only when the deb is absent\n")
for a in A.CATALOG.values():
    if a.kind != "desktop":
        continue
    archs = "/".join(a.arch)
    here = "" if (arch in a.arch or "any" in a.arch) else "  !! no build for this machine"
    chain = " -> ".join(A.substitute_chain(a.id)[1:]) or "-"
    print(f"{a.id:14s} {a.name:18s} [{a.category:8s}] arch={archs:16s} pkg={a.package or '-':24s} fallback: {chain}{here}")
    print(f"               {a.install}")
    if a.note:
        print(f"               note: {a.note}")
print("\nmobile builds: scripts/ubuntu/install_mobile_apps.sh --list   (Waydroid, scripts/ubuntu/setup_waydroid.sh)")
PY
    exit 0 ;;
  --check)
    python3 -c "from cf.linux import apps as A; print(A.catalog_table())"
    exit 0 ;;
  --firefox)
    FIREFOX=1; shift ;;
  "" | -h | --help)
    sed -n '2,10p' "$0"; exit 0 ;;
esac
if [ "$(id -u)" != 0 ]; then echo "run with sudo for installation" >&2; exit 1; fi
export DEBIAN_FRONTEND=noninteractive
arch="$(dpkg --print-architecture)"
installed=(); skipped=(); failed=()

if [ "$FIREFOX" = 1 ]; then
  # Mozilla's APT repository: a real deb (the Ubuntu 22.04 default is a snap whose processes land in
  # snapd's own systemd scope, outside our cgroup).  Same steps as manual §0 step 4.
  echo "== firefox (Mozilla APT deb build) + xclip"
  if dpkg-query -W -f='${db:Status-Status}' firefox 2>/dev/null | grep -q installed && [ ! -d /snap/firefox ] \
     && apt-cache policy firefox | grep -q packages.mozilla.org; then
    echo "   already installed: $(dpkg-query -W -f='${Version}' firefox)"; skipped+=(firefox)
  else
    # Every proxy setting left out: a proxy that is set but fetches nothing (the host's Clash answering
    # "502 Bad Gateway") must not be the end of it - packages.mozilla.org needs none.
    direct=(env -u http_proxy -u https_proxy -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY)
    key=/etc/apt/keyrings/packages.mozilla.org.asc
    # gpg's exit status says nothing here: it is 2 for a root that has no ~/.gnupg yet, with the key shown all the same
    key_ok() { { gpg -n -q --import --import-options import-show "$key" 2>/dev/null || true; } | grep -q 35BAA0B33E9EB396F59CA838C0BA5CE6DC6315A3; }
    # the real thing, not Ubuntu's "firefox" package (a script that starts the snap; its version says so)
    deb_ok() { [ -x /usr/lib/firefox/firefox ] && ! dpkg-query -W -f='${Version}' firefox 2>/dev/null | grep -q snap; }
    get_deb() {  # [direct]
      local pre=() opt=()
      if [ "${1:-}" = direct ]; then pre=("${direct[@]}"); opt=(-o Acquire::http::Proxy= -o Acquire::https::Proxy=); fi
      "${pre[@]}" apt-get "${opt[@]}" update -q || true
      "${pre[@]}" apt-get "${opt[@]}" install -y -q firefox xclip && deb_ok
    }
    install -d -m 0755 /etc/apt/keyrings
    wget -qO "$key" https://packages.mozilla.org/apt/repo-signing-key.gpg || true
    if ! key_ok; then
      echo "   signing key: no luck with the proxy settings as they are, trying without them"
      "${direct[@]}" wget -qO "$key" https://packages.mozilla.org/apt/repo-signing-key.gpg || true
    fi
    if key_ok; then
      echo "deb [signed-by=$key] https://packages.mozilla.org/apt mozilla main" \
        > /etc/apt/sources.list.d/mozilla.list
      printf 'Package: *\nPin: origin packages.mozilla.org\nPin-Priority: 1000\n' > /etc/apt/preferences.d/mozilla
      if get_deb || { echo "   apt: no luck with the proxy settings as they are, trying without them"; get_deb direct; }; then
        # The snap goes only now that the deb is in place - never leave the machine without a browser.
        # Its profile (~/snap/firefox) is not carried over: the deb starts with a fresh ~/.mozilla;
        # snapd keeps a snapshot of the old one for 31 days (snap saved).
        if snap list firefox >/dev/null 2>&1; then
          snap remove firefox || echo "   note: could not remove the firefox snap (is it running? quit it, then: sudo snap remove firefox)"
        fi
        installed+=(firefox)
      else
        echo "!! firefox: the deb from packages.mozilla.org did not install (unreachable with and without the proxy settings;"
        echo "   manual §2.5: python3 -m cf.linux.netcheck). The snap, if there was one, was left alone."
        failed+=(firefox)
      fi
    else
      echo "!! firefox: the Mozilla signing key did not download completely (manual §10) - skipped"; failed+=(firefox)
    fi
  fi
fi

dir="${1:-}"
if [ -z "$dir" ]; then
  [ "$FIREFOX" = 1 ] || sed -n '2,10p' "$0"          # --firefox alone is a complete job
  [ ${#failed[@]} -eq 0 ]
  exit
fi
shopt -s nullglob
debs=("$dir"/*.deb)
if [ ${#debs[@]} -eq 0 ]; then echo "no .deb files in $dir" >&2; exit 1; fi
echo "== $dir: ${#debs[@]} deb(s), this VM is $arch"
for d in "${debs[@]}"; do
  name="$(basename "$d")"
  pkg="$(dpkg-deb -f "$d" Package 2>/dev/null || true)"
  ver="$(dpkg-deb -f "$d" Version 2>/dev/null || true)"
  pkg_arch="$(dpkg-deb -f "$d" Architecture 2>/dev/null || echo '?')"
  if [ -z "$pkg" ]; then
    echo "-- $name: not a deb (an HTML page saved as .deb?) -> skip"; failed+=("$name"); continue
  fi
  if [ "$pkg_arch" != all ] && [ "$pkg_arch" != "$arch" ]; then
    echo "-- $name: built for $pkg_arch, this VM is $arch -> skip (x86_64-only apps cannot run in an arm64 VM)"
    skipped+=("$name"); continue
  fi
  have="$(dpkg-query -W -f='${Version} ${db:Status-Status}' "$pkg" 2>/dev/null || true)"
  if [ "$have" = "$ver installed" ]; then
    echo "-- $name: $pkg $ver is already installed"; skipped+=("$pkg"); continue
  fi
  echo "== $name: installing $pkg $ver"
  # apt wants a path that starts with ./ or /; "./$d" with an absolute $d becomes .//home/... and is
  # rejected ("Unsupported file"), so run apt from inside the directory
  if (cd "$dir" && apt-get install -y -q "./$name") || { dpkg -i "$d" && apt-get -f install -y -q; }; then
    installed+=("$pkg")
  else
    echo "!! $name: install failed"; failed+=("$name")
  fi
done
echo
echo "== installed: ${installed[*]:-none}"
echo "== skipped:   ${skipped[*]:-none}"
[ ${#failed[@]} -eq 0 ] || echo "== FAILED:    ${failed[*]}"
echo "verify with: scripts/ubuntu/install_cn_apps.sh --check ; then python3 run_linux.py CONFIG --discover for the vendor debs"
[ ${#failed[@]} -eq 0 ]
