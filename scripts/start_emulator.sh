#!/usr/bin/env bash
# Boot the AVD with an explicit guest RAM size and wait until Android is up.
#
#   scripts/start_emulator.sh [AVD_NAME=cf_api35] [RAM_MB=6144] [extra emulator args...]
#
# Examples
#   scripts/start_emulator.sh cf_api35 6144                  # default: 6 GB guest (16 GB host)
#   scripts/start_emulator.sh cf_api35 3072 -no-window       # memory-pressure variant, headless
#   GPU=auto scripts/start_emulator.sh                       # let the emulator pick (may drop to software GL)
#
# GPU: the default is `-gpu host`, NOT `-gpu auto`.  With `auto` the emulator silently switches to
# SwiftShader software rendering whenever the host has < 5 GB free ("Software GL rendering will be
# used due to system memory pressure"), which happened with a 6 GB guest on a 16 GB Mac and made
# every app launch ~1 s slower for reasons unrelated to memory.  `host` uses the Apple GPU through
# Metal regardless of host memory; the runner's preflight aborts if software rendering is detected.
#
# Why 6 GB: with 3 GB the Android lmkd kills the compressed background apps long before our
# controller gets to decide anything (dozens of COLD starts per run in earlier logs), and the
# host emulator process itself needs ~1.5-2 GB on top of the guest RAM.  On a 16 GB MacBook
# 6 GB guest + ~2 GB emulator + macOS leaves ~5-6 GB for everything else - close IDE/browser.
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/env.sh

AVD="${1:-cf_api35}"
RAM="${2:-6144}"
shift $(( $# >= 2 ? 2 : $# )) || true

LOG="results/emulator_${AVD}.log"
mkdir -p results

# ---- host memory sanity check (the emulator silently drops to software rendering when the
#      host is short of memory, which makes every launch 3-10x slower)
host_avail_mb() {
  case "$(uname -s)" in
    Darwin)
      local page free inactive spec
      page=$(sysctl -n hw.pagesize)
      free=$(vm_stat | awk '/Pages free/ {gsub("\\.","",$3); print $3}')
      inactive=$(vm_stat | awk '/Pages inactive/ {gsub("\\.","",$3); print $3}')
      spec=$(vm_stat | awk '/Pages speculative/ {gsub("\\.","",$3); print $3}')
      echo $(( (free + inactive + spec) * page / 1048576 ));;
    Linux) free -m | awk '/^Mem:/ {print $7}';;
    *) echo 0;;
  esac
}
host_total_mb() {
  case "$(uname -s)" in
    Darwin) echo $(( $(sysctl -n hw.memsize) / 1048576 ));;
    Linux) free -m | awk '/^Mem:/ {print $2}';;
    *) echo 0;;
  esac
}
AVAIL=$(host_avail_mb); TOTAL=$(host_total_mb)
NEED=$(( RAM + 2048 ))
echo "== host memory: ${AVAIL} MB available of ${TOTAL} MB; guest ${RAM} MB + ~2 GB emulator overhead = ${NEED} MB"
if [ "$AVAIL" -gt 0 ] && [ "$AVAIL" -lt "$NEED" ]; then
  echo "!! only ${AVAIL} MB available on the host. Close Android Studio / browsers / other VMs first,"
  echo "   or start with less guest RAM (scripts/start_emulator.sh $AVD 4096). Continuing in 5 s..."
  sleep 5
fi
if [ "$TOTAL" -gt 0 ] && [ "$RAM" -gt $(( TOTAL / 2 )) ]; then
  echo "!! guest RAM ${RAM} MB is more than half of the host (${TOTAL} MB) - expect host swapping"
fi

if pgrep -f "qemu-system.*-avd $AVD" >/dev/null 2>&1; then
  echo "!! an emulator for $AVD is already running (results/emulator.pid); kill it first:  kill \$(cat results/emulator.pid)"
  exit 1
fi

GPU="${GPU:-host}"
echo "== starting $AVD with ${RAM} MB RAM, 4 cores, -gpu $GPU (log: $LOG)"
nohup "$EMULATOR" -avd "$AVD" -memory "$RAM" -cores 4 -no-snapshot -no-boot-anim \
  -no-audio -gpu "$GPU" -netdelay none -netspeed full "$@" > "$LOG" 2>&1 &
echo $! > results/emulator.pid

"$ADB" wait-for-device
echo -n "== waiting for boot"
for _ in $(seq 1 240); do
  if [ "$("$ADB" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ]; then
    echo; echo "== booted: $("$ADB" shell getprop ro.build.fingerprint | tr -d '\r')"
    GUEST_KB=$("$ADB" shell grep MemTotal /proc/meminfo | tr -d '\r' | awk '{print $2}')
    echo "== guest MemTotal $(( GUEST_KB / 1024 )) MB (requested ${RAM}), kernel $("$ADB" shell uname -r | tr -d '\r')"
    if [ $(( GUEST_KB / 1024 )) -lt $(( RAM * 9 / 10 )) ]; then
      echo "!! guest sees much less than ${RAM} MB - is hw.ramSize capped in config.ini or by a -memory flag elsewhere?"
    fi
    sleep 2
    if grep -qi "software gl\|swiftshader\|llvmpipe" "$LOG"; then
      echo "!! the emulator fell back to SOFTWARE rendering (see $LOG). Resume latencies will be"
      echo "   dominated by the CPU rasteriser and run_experiment.py will refuse to start (strict preflight)."
      echo "   Free host memory (close IDE/browser) and restart; GPU=$GPU was requested."
    else
      R="$("$ADB" shell dumpsys SurfaceFlinger 2>/dev/null | grep -m1 'GLES:' | tr -d '\r' | cut -c1-110)"
      echo "== renderer: $R"
      case "$R" in *[Ss]wift[Ss]hader*|*llvmpipe*) echo "!! guest reports a software renderer; check $LOG";; esac
    fi
    exit 0
  fi
  echo -n .; sleep 2
done
echo; echo "boot timed out; see $LOG"; exit 1
