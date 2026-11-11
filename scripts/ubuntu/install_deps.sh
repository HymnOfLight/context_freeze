#!/usr/bin/env bash
# Packages the Ubuntu back-end needs (run inside the VM):
#     sudo scripts/ubuntu/install_deps.sh            # tools + python deps
#     sudo scripts/ubuntu/install_deps.sh --subs     # also the open-source substitutes (kdenlive, librecad, gimp)
#     sudo scripts/ubuntu/install_deps.sh --hwe      # also the HWE kernel (memory.reclaim needs >= 5.19)
set -euo pipefail
if [ "$(id -u)" != 0 ]; then echo "run with sudo" >&2; exit 1; fi
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q xdotool xclip x11-utils x11-xserver-utils python3-xlib python3-pip python3-numpy \
  python3-matplotlib python3-openpyxl dbus-x11 libnotify-bin util-linux wmctrl procps git iproute2
# a Chinese font that also has letters and digits, for the figure labels (a minimal install has only
# Droid Sans Fallback, which has neither; cf/linux/plots.py copes with it, this one looks better)
apt-get install -y -q fonts-wqy-microhei || echo "!! fonts-wqy-microhei not installed: figures use whatever CJK font there is" >&2
# the compute apps of the three-class experiment (manual §6.9) drive these real tools
apt-get install -y -q p7zip-full z3
for a in "$@"; do
  case "$a" in
    --subs) apt-get install -y -q kdenlive librecad gimp ;;
    --hwe)  apt-get install -y -q "linux-generic-hwe-22.04" && echo "== reboot to run the HWE kernel" ;;
  esac
done
cd "$(dirname "$0")/../.."
if [ -n "${SUDO_USER:-}" ]; then
  sudo -u "$SUDO_USER" python3 -m pip install -q --user -r requirements.txt || true
fi
echo "== done. Session type: ${XDG_SESSION_TYPE:-?} (must be x11: pick 'Ubuntu on Xorg' at login)"
echo "   kernel: $(uname -r)  memory.reclaim: $( [ -e /sys/fs/cgroup/memory.reclaim ] && echo yes || echo no)"
