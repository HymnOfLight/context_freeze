#!/usr/bin/env bash
# One-time device preparation (needs a rootable image):
#   * adb root
#   * enable / disable Android's own cached-apps freezer (baseline vs. our controller)
#   * zram swap of the requested size (compressed tier), optional swap file on /data ("flash" tier)
#   * disable animations, keep screen on, dismiss keyguard
#
#   scripts/prepare_device.sh [--system-freezer enabled|disabled] [--zram-mb 1024] [--swapfile-mb 0] [--no-reboot]
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/env.sh

SYS_FREEZER="disabled"; ZRAM_MB=1024; SWAPFILE_MB=0; REBOOT=1
while [ $# -gt 0 ]; do
  case "$1" in
    --system-freezer) SYS_FREEZER="$2"; shift 2;;
    --zram-mb) ZRAM_MB="$2"; shift 2;;
    --swapfile-mb) SWAPFILE_MB="$2"; shift 2;;
    --no-reboot) REBOOT=0; shift;;
    *) echo "unknown arg $1"; exit 1;;
  esac
done

sh() { "$ADB" shell "$@" | tr -d '\r'; }

echo "== adb root"
"$ADB" root >/dev/null 2>&1 || true
"$ADB" wait-for-device
if [ "$(sh id -u)" != "0" ]; then
  echo "!! adb root failed. Use a google_apis or default (AOSP) image, not google_apis_playstore."; exit 1
fi

CUR="$(sh settings get global cached_apps_freezer)"
echo "== cached_apps_freezer: current='$CUR' wanted='$SYS_FREEZER'"
if [ "$CUR" != "$SYS_FREEZER" ]; then
  sh settings put global cached_apps_freezer "$SYS_FREEZER"
  if [ "$SYS_FREEZER" = "disabled" ]; then
    sh device_config put activity_manager use_freezer false >/dev/null 2>&1 || true
  else
    sh device_config put activity_manager use_freezer true >/dev/null 2>&1 || true
  fi
  if [ "$REBOOT" = 1 ]; then
    echo "== rebooting for the freezer setting to take effect"
    "$ADB" reboot; "$ADB" wait-for-device
    until [ "$(sh getprop sys.boot_completed 2>/dev/null)" = "1" ]; do sleep 2; done
    "$ADB" root >/dev/null 2>&1 || true; "$ADB" wait-for-device; sleep 2
  fi
fi

echo "== kernel $(sh uname -r), $(sh grep MemTotal /proc/meminfo)"
echo "== cgroup v2 controllers: $(sh cat /sys/fs/cgroup/cgroup.controllers 2>/dev/null || echo none)"
echo "== freezer dirs: $(sh ls /sys/fs/cgroup | grep -c uid_ 2>/dev/null || echo 0) uid_* groups"
echo "== per-process reclaim: $(sh '[ -e /proc/self/reclaim ] && echo /proc/pid/reclaim || echo no')"

if [ "$ZRAM_MB" -gt 0 ]; then
  if sh cat /proc/swaps | grep -q zram; then
    echo "== zram already active: $(sh cat /proc/swaps | grep zram)"
  else
    sh '[ -e /sys/block/zram0 ] || modprobe zram 2>/dev/null; echo' >/dev/null
    if sh '[ -e /sys/block/zram0 ] && echo yes' | grep -q yes; then
      echo "== enabling zram ${ZRAM_MB} MB (algorithms: $(sh cat /sys/block/zram0/comp_algorithm))"
      sh "echo 1 > /sys/block/zram0/reset; echo ${ZRAM_MB}M > /sys/block/zram0/disksize && mkswap /dev/block/zram0 >/dev/null && (swapon -p 32767 /dev/block/zram0 2>/dev/null || swapon /dev/block/zram0)"
    else
      echo "!! no zram device in this kernel; only a swap file is possible (--swapfile-mb)"
    fi
  fi
fi
if [ "$SWAPFILE_MB" -gt 0 ]; then
  F=/data/local/tmp/cf_swapfile
  if ! sh cat /proc/swaps | grep -q "$F"; then
    echo "== creating swap file ${SWAPFILE_MB} MB on /data (flash tier)"
    sh "dd if=/dev/zero of=$F bs=1048576 count=${SWAPFILE_MB} 2>/dev/null; chmod 600 $F; mkswap $F >/dev/null; swapon -p 100 $F 2>/dev/null || swapon $F"
  fi
fi
echo "== /proc/swaps:"; sh cat /proc/swaps

sh 'settings put global window_animation_scale 0; settings put global transition_animation_scale 0; settings put global animator_duration_scale 0'
sh 'svc power stayon true; input keyevent KEYCODE_WAKEUP; wm dismiss-keyguard' >/dev/null 2>&1 || true
sh 'settings put global stay_on_while_plugged_in 7' >/dev/null 2>&1 || true
echo "== done. launchable packages: scripts/list_launchable.sh"
