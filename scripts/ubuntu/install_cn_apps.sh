#!/usr/bin/env bash
# Install the mainland-China desktop apps from a directory of .deb files you downloaded from the
# vendors (the download pages are printed by --list; most of them need a browser click-through or a
# login, so this is not automated):
#
#     scripts/ubuntu/install_cn_apps.sh --list            # per app: download page, arch, package name
#     sudo scripts/ubuntu/install_cn_apps.sh ~/Downloads   # apt install ./*.deb (resolves dependencies)
#     scripts/ubuntu/install_cn_apps.sh --check           # what the runner will consider installed
#
# After installing, start each app once by hand (scripts/ubuntu/cf_launch.sh <id>), accept the
# agreements, log in, and run `python3 run_linux.py configs/ubuntu_cn_desktop.json --discover` to
# verify the WM_CLASS / command names the catalogue assumes (override in app_overrides if they differ).
set -euo pipefail
cd "$(dirname "$0")/../.."
case "${1:-}" in
  --list)
    python3 - <<'PY'
from cf.linux import apps as A
for a in A.CATALOG.values():
    if a.id.startswith("sim_") or a.id == "xiaoyi_sim":
        continue
    arch = "/".join(a.arch)
    print(f"{a.id:14s} {a.name:18s} [{a.category:8s}] arch={arch:16s} pkg={a.package or '-'}")
    print(f"               {a.install}")
    if a.note:
        print(f"               note: {a.note}")
PY
    exit 0 ;;
  --check)
    python3 -c "from cf.linux import apps as A; print(A.catalog_table())"
    exit 0 ;;
  "" | -h | --help)
    sed -n '2,12p' "$0"; exit 0 ;;
esac
if [ "$(id -u)" != 0 ]; then echo "run with sudo for installation" >&2; exit 1; fi
dir="$1"
shopt -s nullglob
debs=("$dir"/*.deb)
if [ ${#debs[@]} -eq 0 ]; then echo "no .deb files in $dir" >&2; exit 1; fi
export DEBIAN_FRONTEND=noninteractive
arch="$(dpkg --print-architecture)"
for d in "${debs[@]}"; do
  pkg_arch="$(dpkg-deb -f "$d" Architecture 2>/dev/null || echo '?')"
  if [ "$pkg_arch" != all ] && [ "$pkg_arch" != "$arch" ]; then
    echo "!! skip $(basename "$d"): built for $pkg_arch, this VM is $arch (x86_64-only apps cannot run in an arm64 VM)"
    continue
  fi
  echo "== installing $(basename "$d")"
  apt-get install -y -q "./$d" || dpkg -i "$d" || true
done
apt-get -f install -y -q || true
echo "== installed; verify with: scripts/ubuntu/install_cn_apps.sh --check"
