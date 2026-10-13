#!/usr/bin/env bash
# Waydroid on the Ubuntu 22.04 (Xorg) desktop: the Android runtime for the apps that have no
# Ubuntu desktop build (剪映, 小艺) or whose deb you could not get for this architecture.
#
#   sudo scripts/ubuntu/setup_waydroid.sh --install     # once: repo.waydro.id + waydroid + binder modules, waydroid init
#   scripts/ubuntu/setup_waydroid.sh                    # every login: nested weston (X11 window) + session + adb connect
#   scripts/ubuntu/setup_waydroid.sh --status           # waydroid status / adb devices / ABI
#   scripts/ubuntu/setup_waydroid.sh --stop             # stop the session and weston
#
# Why nested weston: the experiments run on an Xorg session (xdotool cannot switch native Wayland
# windows), but Waydroid draws on a Wayland display.  A weston started with the X11 backend is a
# normal X11 window that xdotool can activate; Waydroid's session is started with
# WAYLAND_DISPLAY pointing at that weston.  All Android apps share this one window; the runner
# switches apps inside it with `am start -W` (cf.linux.mobile).
#
# Why Waydroid and not an emulator: Waydroid is an LXC container on the HOST kernel, so Android
# app processes are visible in /proc and can be moved into /sys/fs/cgroup/cf/<app> - the same
# throttling as for desktop apps.  (A nested-virtualisation emulator would hide them.)  arm64 APKs
# run natively on the arm64 VM; on an x86_64 VM arm64-only APKs need the libhoudini / libndk
# translation image (waydroid init -s GAPPS is not needed - the CN apps do not use GMS).
set -euo pipefail
WAYLAND_SOCKET="${CF_WAYDROID_WAYLAND:-waydroid}"
WESTON_SIZE="${CF_WESTON_SIZE:-1280x800}"
ADB_PORT=5555

case "${1:-}" in
  --install)
    if [ "$(id -u)" != 0 ]; then echo "run with sudo for --install" >&2; exit 1; fi
    export DEBIAN_FRONTEND=noninteractive
    apt-get install -y -q curl ca-certificates weston adb "linux-modules-extra-$(uname -r)" || true
    curl -fsSL https://repo.waydro.id | bash -s "$(. /etc/os-release; echo "${UBUNTU_CODENAME:-jammy}")"
    apt-get install -y -q waydroid
    if ! modprobe binder_linux devices=binder,hwbinder,vndbinder 2>/dev/null && [ ! -e /dev/binderfs ]; then
      echo "!! no binder module in this kernel: install linux-modules-extra for the running kernel (or the HWE kernel" >&2
      echo "   via install_deps.sh --hwe), reboot, and re-run --install" >&2
    fi
    echo "== waydroid init (downloads the LineageOS image, ~1 GB; VANILLA = no Google apps)"
    waydroid init -s VANILLA || waydroid init
    systemctl enable --now waydroid-container
    # one Android window, Android decides the activity stack; the runner only activates the X11 window
    waydroid prop set persist.waydroid.multi_windows false || true
    waydroid prop set persist.waydroid.width 1280 || true
    waydroid prop set persist.waydroid.height 800 || true
    # sudoers rule so the runner can move Android app processes into /cf/<app> without a password
    user="${SUDO_USER:-}"
    repo="$(cd "$(dirname "$0")/../.." && pwd)"
    if [ -n "$user" ]; then
      echo "$user ALL=(root) NOPASSWD: $repo/scripts/ubuntu/cf_adopt.sh" > /etc/sudoers.d/cf-adopt
      chmod 0440 /etc/sudoers.d/cf-adopt
      echo "== /etc/sudoers.d/cf-adopt written (set \"sudo_adopt\": true in the config)"
    fi
    echo "== done. Log in to the Xorg desktop and run: scripts/ubuntu/setup_waydroid.sh"
    exit 0 ;;
  --status)
    waydroid status || true
    adb devices -l || true
    ip="$(waydroid status 2>/dev/null | awk -F': *' '/IP address/{print $2}')"
    [ -n "$ip" ] && adb -s "$ip:$ADB_PORT" shell getprop ro.product.cpu.abi 2>/dev/null || true
    pgrep -a weston || echo "weston: not running"
    exit 0 ;;
  --stop)
    waydroid session stop || true
    pkill -f "weston --backend=x11-backend.so --socket=$WAYLAND_SOCKET" || true
    exit 0 ;;
  -h | --help)
    sed -n '2,10p' "$0"; exit 0 ;;
esac

if [ -z "${DISPLAY:-}" ]; then echo "no DISPLAY: run from a terminal on the Xorg desktop" >&2; exit 1; fi
if [ "${XDG_SESSION_TYPE:-}" = wayland ]; then
  echo "!! this is a Wayland session; the runner needs 'Ubuntu on Xorg' (gear icon at the login screen)" >&2
fi
XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export XDG_RUNTIME_DIR
if ! pgrep -f "weston --backend=x11-backend.so --socket=$WAYLAND_SOCKET" >/dev/null; then
  echo "== starting nested weston (X11 window) on wayland socket $WAYLAND_SOCKET"
  nohup weston --backend=x11-backend.so --socket="$WAYLAND_SOCKET" --width="${WESTON_SIZE%x*}" \
    --height="${WESTON_SIZE#*x}" --xwayland >/tmp/cf_weston.log 2>&1 &
  for _ in $(seq 1 20); do [ -S "$XDG_RUNTIME_DIR/$WAYLAND_SOCKET" ] && break; sleep 0.5; done
fi
[ -S "$XDG_RUNTIME_DIR/$WAYLAND_SOCKET" ] || { echo "weston did not create $XDG_RUNTIME_DIR/$WAYLAND_SOCKET (see /tmp/cf_weston.log)" >&2; exit 1; }
if ! waydroid status 2>/dev/null | grep -q "Session:.*RUNNING"; then
  echo "== starting the waydroid session on WAYLAND_DISPLAY=$WAYLAND_SOCKET"
  WAYLAND_DISPLAY="$WAYLAND_SOCKET" nohup waydroid session start >/tmp/cf_waydroid_session.log 2>&1 &
  for _ in $(seq 1 60); do waydroid status 2>/dev/null | grep -q "Session:.*RUNNING" && break; sleep 2; done
fi
waydroid status
ip="$(waydroid status 2>/dev/null | awk -F': *' '/IP address/{print $2}')"
if [ -n "$ip" ]; then
  adb connect "$ip:$ADB_PORT" || true
  echo "== adb: $(adb -s "$ip:$ADB_PORT" shell getprop ro.product.cpu.abi 2>/dev/null || echo 'not connected (enable adb: waydroid prop set persist.sys.usb.config adb; or use sudo waydroid shell)')"
  echo "   put \"mobile\": {\"adb_serial\": \"$ip:$ADB_PORT\"} in the config if it differs from the default"
fi
echo "== Android window: $(xdotool search --onlyvisible --class weston 2>/dev/null | head -1 || echo '?') (xdotool search --class weston)"
echo "== next: scripts/ubuntu/install_mobile_apps.sh <packages>; python3 run_linux.py <config> --mobile-status"
