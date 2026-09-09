#!/usr/bin/env bash
# Boot the AVD with an explicit guest RAM size and wait until Android is up.
#
#   scripts/start_emulator.sh [AVD_NAME=cf_api35] [RAM_MB=3072] [extra emulator args...]
#
# Examples
#   scripts/start_emulator.sh cf_api35 3072                  # baseline pressure
#   scripts/start_emulator.sh cf_api35 2048 -no-window       # high pressure, headless
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/env.sh

AVD="${1:-cf_api35}"
RAM="${2:-3072}"
shift $(( $# >= 2 ? 2 : $# )) || true

LOG="results/emulator_${AVD}.log"
mkdir -p results
echo "== starting $AVD with ${RAM} MB RAM (log: $LOG)"
nohup "$EMULATOR" -avd "$AVD" -memory "$RAM" -cores 4 -no-snapshot -no-boot-anim \
  -no-audio -gpu auto -netdelay none -netspeed full "$@" > "$LOG" 2>&1 &
echo $! > results/emulator.pid

"$ADB" wait-for-device
echo -n "== waiting for boot"
for _ in $(seq 1 180); do
  if [ "$("$ADB" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ]; then
    echo; echo "== booted: $("$ADB" shell getprop ro.build.fingerprint | tr -d '\r')"
    "$ADB" shell "cat /proc/meminfo | head -3; uname -r"
    exit 0
  fi
  echo -n .; sleep 2
done
echo; echo "boot timed out; see $LOG"; exit 1
