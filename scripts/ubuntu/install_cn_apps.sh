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
    install -d -m 0755 /etc/apt/keyrings
    wget -qO /etc/apt/keyrings/packages.mozilla.org.asc https://packages.mozilla.org/apt/repo-signing-key.gpg || true
    if gpg -n -q --import --import-options import-show /etc/apt/keyrings/packages.mozilla.org.asc 2>/dev/null \
         | grep -q 35BAA0B33E9EB396F59CA838C0BA5CE6DC6315A3; then
      echo "deb [signed-by=/etc/apt/keyrings/packages.mozilla.org.asc] https://packages.mozilla.org/apt mozilla main" \
        > /etc/apt/sources.list.d/mozilla.list
      printf 'Package: *\nPin: origin packages.mozilla.org\nPin-Priority: 1000\n' > /etc/apt/preferences.d/mozilla
      snap list firefox >/dev/null 2>&1 && snap remove firefox
      if apt-get update -q && apt-get install -y -q firefox xclip; then installed+=(firefox)
      else echo "!! firefox: apt failed (packages.mozilla.org unreachable? proxy on for this step: manual §2.5)"; failed+=(firefox); fi
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
