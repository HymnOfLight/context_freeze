#!/usr/bin/env bash
# Enlarge the /data partition of an existing AVD.
#
#   scripts/resize_data.sh [AVD=cf_api35] [GB=16] [RAM_MB=6144]
#
# The emulator only applies disk.dataPartition.size when the userdata image is (re)created, so this
# WIPES /data: every installed app, its data and logins, and the zram / freezer settings of
# prepare_device.sh are gone. What survives is on the host: the APKs cached in apks/<pkg>/ by
# install_cn_apps.py, so re-provisioning is a few minutes of installing, not downloading:
#
#   scripts/resize_data.sh cf_api35 16
#   scripts/prepare_device.sh --system-freezer disabled --zram-mb 2048
#   python3 scripts/install_cn_apps.py --download --config configs/cn_apps.json
#   scripts/first_run_cn_apps.sh configs/cn_apps.json
#
# (or just re-run `bash scripts/bootstrap_cn.sh`, which does all of that.)
# YES=1 skips the confirmation prompt.
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/env.sh

AVD="${1:-cf_api35}"; GB="${2:-16}"; RAM="${3:-6144}"
DIR="$HOME/.android/avd/$AVD.avd"; INI="$DIR/config.ini"
[ -f "$INI" ] || { echo "!! no AVD $AVD ($INI)"; exit 1; }
CUR=$(grep '^disk.dataPartition.size=' "$INI" | cut -d= -f2 || true)

echo "== $AVD: disk.dataPartition.size ${CUR:-<default>} -> ${GB}G"
echo "   This wipes /data of the emulator (apps, app data, logins, zram/freezer settings)."
echo "   APKs cached in apks/ are kept; results/ is on the host and untouched."
if [ "${YES:-0}" != 1 ]; then
  read -r -p "   continue? [y/N] " a
  case "$a" in y|Y|yes|YES) ;; *) echo "aborted"; exit 1;; esac
fi

if pgrep -f "qemu-system.*-avd $AVD" >/dev/null 2>&1; then
  echo "== stopping the running emulator"
  "$ADB" emu kill >/dev/null 2>&1 || true
  for _ in $(seq 1 30); do pgrep -f "qemu-system.*-avd $AVD" >/dev/null 2>&1 || break; sleep 1; done
  pgrep -f "qemu-system.*-avd $AVD" >/dev/null 2>&1 && pkill -f "qemu-system.*-avd $AVD" && sleep 2
fi

if grep -q '^disk.dataPartition.size=' "$INI"; then
  sed -i.bak "s|^disk.dataPartition.size=.*|disk.dataPartition.size=${GB}G|" "$INI" && rm -f "$INI.bak"
else
  echo "disk.dataPartition.size=${GB}G" >> "$INI"
fi

echo "== booting with -wipe-data (userdata is recreated at ${GB} GB)"
scripts/start_emulator.sh "$AVD" "$RAM" -wipe-data
echo "== /data now: $("$ADB" shell df -h /data | tail -1 | tr -d '\r')"
echo "   next: scripts/prepare_device.sh --system-freezer disabled --zram-mb 2048"
echo "         python3 scripts/install_cn_apps.py --download --config configs/cn_apps.json"
echo "         scripts/first_run_cn_apps.sh configs/cn_apps.json"
