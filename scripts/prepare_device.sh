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
# what system_server actually uses right now (CachedAppOptimizer reads the setting at boot)
ACTIVE="$(sh dumpsys activity settings 2>/dev/null | grep -o 'use_freezer=[a-z]*' | head -1 || true)"
echo "== cached_apps_freezer: setting='$CUR' active='${ACTIVE:-unknown}' wanted='$SYS_FREEZER'"
WANT_ACTIVE="use_freezer=true"; [ "$SYS_FREEZER" = "disabled" ] && WANT_ACTIVE="use_freezer=false"
if [ "$CUR" != "$SYS_FREEZER" ] || { [ -n "$ACTIVE" ] && [ "$ACTIVE" != "$WANT_ACTIVE" ]; }; then
  sh settings put global cached_apps_freezer "$SYS_FREEZER"
  FLAG=true; [ "$SYS_FREEZER" = "disabled" ] && FLAG=false
  # Android 11+ reads the flag from the *_native_boot namespace; older builds from activity_manager
  sh device_config put activity_manager_native_boot use_freezer $FLAG >/dev/null 2>&1 || true
  sh device_config put activity_manager use_freezer $FLAG >/dev/null 2>&1 || true
  # CachedAppOptimizer also *compacts* cached apps into zram on its own (that is where the ~200 MB of
  # SwapPss in the `none` runs came from); it must go off together with the freezer for a clean baseline
  sh device_config put activity_manager_native_boot use_compaction $FLAG >/dev/null 2>&1 || true
  sh device_config put activity_manager use_compaction $FLAG >/dev/null 2>&1 || true
  # keep the flags across reboots / device_config syncs
  sh device_config set_sync_disabled_for_tests persistent >/dev/null 2>&1 || true
  if [ "$REBOOT" = 1 ]; then
    echo "== rebooting for the freezer setting to take effect"
    "$ADB" reboot; "$ADB" wait-for-device
    until [ "$(sh getprop sys.boot_completed 2>/dev/null)" = "1" ]; do sleep 2; done
    "$ADB" root >/dev/null 2>&1 || true; "$ADB" wait-for-device; sleep 3
    ACTIVE="$(sh dumpsys activity settings 2>/dev/null | grep -o 'use_freezer=[a-z]*' | head -1 || true)"
    COMPACT="$(sh dumpsys activity settings 2>/dev/null | grep -o 'use_compaction=[a-z]*' | head -1 || true)"
    if [ -n "$ACTIVE" ] && [ "$ACTIVE" != "$WANT_ACTIVE" ]; then
      echo "!! system freezer still reports '$ACTIVE' after reboot - it will fight our controller for cgroup.freeze"
    else
      echo "== system freezer now: ${ACTIVE:-unknown (dumpsys did not report use_freezer)}, ${COMPACT:-use_compaction=?}"
    fi
    echo "== settings get global cached_apps_freezer -> $(sh settings get global cached_apps_freezer)"
  else
    echo "!! --no-reboot: the new freezer setting only applies after the next reboot"
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
echo "== kill diagnostics: the runner records \`dumpsys activity exit-info <pkg>\` for every process that dies;"
echo "   for a live view run:  adb logcat -b events -b system | grep -E 'am_kill|am_proc_died|am_anr'"
echo "== done. launchable packages: scripts/list_launchable.sh"
