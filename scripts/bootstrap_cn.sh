#!/usr/bin/env bash
# One-shot, re-runnable pipeline for the mainland-China app experiments on a Mac (Apple Silicon):
#
#   SDK + AVD (6 GB guest, 16 GB /data) -> boot -> device prep (system freezer off, 2 GB zram)
#   -> install the apps from your phone (or apks/) -> first-run pass (manual, once)
#   -> probe -> scenario x policy x eta x seed matrix -> summary + Pareto plots
#
#   cd ~/context_freeze && bash scripts/bootstrap_cn.sh
#
# Every stage checks whether it is already done, so re-running the same command after an
# interruption (Ctrl+C, reboot, emulator crash) continues where it stopped: the matrix itself is
# checkpointed per step (docs/02 §6). The only stages that need you are (1) plugging in the phone
# with USB debugging on and (2) tapping through privacy agreements / logins once.
#
# Knobs (environment variables):
#   QUICK=1                       smoke test: one scenario, one seed, T=20 (~5 min instead of ~30 h)
#   SCENARIOS="office social ..." default: office social commute shopping evening
#   POLICIES="none lru landlord hybrid"   ETAS="0.25 0.35 0.5"   SEEDS="1 2 3"   T=60
#   OUT=results/cn_matrix         matrix directory (fixed so that re-runs resume)
#   DATA_GB=16                    /data partition of a *new* AVD (the default 8 GB is too small)
#   SKIP_FIRST_RUN=1              do not run the interactive first-run pass again
#   PHONE=<serial>                skip phone auto-detection
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$PWD"

AVD="${AVD:-cf_api35}"; RAM_MB="${RAM_MB:-6144}"; DATA_GB="${DATA_GB:-16}"
ZRAM_MB="${ZRAM_MB:-2048}"; CONFIG="${CONFIG:-configs/cn_apps.json}"
OUT="${OUT:-results/cn_matrix}"; T="${T:-60}"
SCENARIOS="${SCENARIOS:-office social commute shopping evening}"
POLICIES="${POLICIES:-none lru landlord hybrid}"; ETAS="${ETAS:-0.25 0.35 0.5}"; SEEDS="${SEEDS:-1 2 3}"
if [ "${QUICK:-0}" = 1 ]; then
  SCENARIOS="office"; POLICIES="none landlord"; ETAS="0.3"; SEEDS="1"; T=20; OUT="results/cn_quick"
fi
export ANDROID_SERIAL="${ANDROID_SERIAL:-emulator-5554}"

step() { printf '\n\033[1m==== %s\033[0m\n' "$*"; }
die()  { printf '\033[31m!! %s\033[0m\n' "$*"; exit 1; }
sh_()  { "$ADB" shell "$@" | tr -d '\r'; }

# ------------------------------------------------------------------ 0. host tools
step "0/7 host tools (Homebrew, Java, Android cmdline-tools, Python deps)"
[ "$(uname -s)" = Darwin ] || echo "   (not macOS - SDK install steps are skipped, the rest works on Linux too)"
if [ "$(uname -s)" = Darwin ]; then
  command -v brew >/dev/null || die "Homebrew missing: /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
  if ! /usr/libexec/java_home -v 17+ >/dev/null 2>&1; then
    brew install --quiet openjdk@17
    sudo ln -sfn /opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk /Library/Java/JavaVirtualMachines/openjdk-17.jdk 2>/dev/null || true
  fi
  export JAVA_HOME="$(/usr/libexec/java_home -v 17+ 2>/dev/null || echo /opt/homebrew/opt/openjdk@17)"
  source scripts/env.sh
  if [ -z "$SDKMANAGER" ]; then
    brew install --quiet --cask android-commandlinetools
    source scripts/env.sh
  fi
  [ -n "$SDKMANAGER" ] || die "sdkmanager still not found; install Android Studio > SDK Tools > Command-line Tools"
else
  source scripts/env.sh
fi
python3 -m pip install -q -r requirements.txt 2>/dev/null || python3 -m pip install -q --user -r requirements.txt

# ------------------------------------------------------------------ 1. AVD
step "1/7 AVD $AVD ($RAM_MB MB RAM, ${DATA_GB} GB /data)"
INI="$HOME/.android/avd/$AVD.avd/config.ini"
if [ ! -f "$INI" ]; then
  scripts/setup_avd.sh 35 "$AVD" "$RAM_MB"
  # the data partition size is fixed when the disk is first created: set it before the first boot
  sed -i '' "s/^disk.dataPartition.size=.*/disk.dataPartition.size=${DATA_GB}G/" "$INI" 2>/dev/null \
    || sed -i "s/^disk.dataPartition.size=.*/disk.dataPartition.size=${DATA_GB}G/" "$INI"
  grep -q "^disk.dataPartition.size=" "$INI" || echo "disk.dataPartition.size=${DATA_GB}G" >> "$INI"
else
  echo "   exists: $INI ($(grep '^disk.dataPartition.size' "$INI" || echo 'dataPartition default'))"
fi

# ------------------------------------------------------------------ 2. boot
step "2/7 emulator"
if [ "$(sh_ getprop sys.boot_completed 2>/dev/null)" = "1" ]; then
  echo "   already running: $ANDROID_SERIAL"
else
  scripts/start_emulator.sh "$AVD" "$RAM_MB"
fi
FREE_DATA=$(sh_ df -m /data | awk 'NR==2{print $4}')
echo "   /data free: ${FREE_DATA} MB"
if [ "${FREE_DATA:-0}" -lt 6000 ] && ! sh_ pm list packages | grep -q com.tencent.mm; then
  echo "!! less than 6 GB free on /data before installing the apps. This AVD was created with the 8 GB default."
  echo "   Recreate it with a bigger partition (deletes the emulator's data):"
  echo "     \$ADB emu kill; \$AVDMANAGER delete avd -n $AVD; DATA_GB=16 bash scripts/bootstrap_cn.sh"
  die "not enough space on /data"
fi

# ------------------------------------------------------------------ 3. device prep
step "3/7 device preparation (system freezer/compaction off, zram ${ZRAM_MB} MB)"
if [ "$(sh_ settings get global cached_apps_freezer)" = "disabled" ] && sh_ cat /proc/swaps | grep -q zram; then
  echo "   already prepared"
else
  scripts/prepare_device.sh --system-freezer disabled --zram-mb "$ZRAM_MB"
fi

# ------------------------------------------------------------------ 4. apps
step "4/7 apps ($CONFIG)"
WANT=$(python3 -c "import json;print(' '.join(json.load(open('$CONFIG'))['apps']))")
HAVE=$(sh_ pm list packages | sed 's/^package://')
MISSING=(); for p in $WANT; do grep -qx "$p" <<<"$HAVE" || MISSING+=("$p"); done
if [ ${#MISSING[@]} -eq 0 ]; then
  echo "   all $(wc -w <<<"$WANT") apps installed"
else
  echo "   missing ${#MISSING[@]}: ${MISSING[*]}"
  if [ -z "${PHONE:-}" ]; then
    PHONE=$("$ADB" devices | awk 'NR>1 && $2=="device" && $1 !~ /^emulator-/ {print $1; exit}')
  fi
  if [ -z "$PHONE" ] && ! ls apks/*.apk apks/*.xapk apks/*.apks apks/*/ >/dev/null 2>&1; then
    echo
    echo "   Connect your phone now: 设置 > 关于手机 > 连点版本号 7 次 -> 开发者选项 > USB 调试; plug in; tap 允许."
    echo "   (or drop .apk/.xapk files into $REPO/apks/ - download pages: python3 scripts/install_cn_apps.py --list)"
    for i in $(seq 1 60); do
      PHONE=$("$ADB" devices | awk 'NR>1 && $2=="device" && $1 !~ /^emulator-/ {print $1; exit}')
      [ -n "$PHONE" ] && break
      ls apks/*.apk apks/*.xapk apks/*.apks apks/*/ >/dev/null 2>&1 && break
      printf '.'; sleep 5
    done; echo
  fi
  if [ -n "$PHONE" ]; then
    echo "   phone: $PHONE"
    python3 scripts/install_cn_apps.py --from-phone "$PHONE" --config "$CONFIG"
  elif ls apks/*.apk apks/*.xapk apks/*.apks apks/*/ >/dev/null 2>&1; then
    python3 scripts/install_cn_apps.py
  else
    die "no phone and no APKs in apks/; re-run when either is available"
  fi
fi

# ------------------------------------------------------------------ 5. first run
step "5/7 first-run pass (privacy agreements / logins)"
MARK="results/.first_run_done_$AVD"
if [ "${SKIP_FIRST_RUN:-0}" = 1 ] || [ -f "$MARK" ]; then
  echo "   done before ($MARK); delete the file to repeat"
else
  scripts/first_run_cn_apps.sh "$CONFIG"
  mkdir -p results && touch "$MARK"
fi

# ------------------------------------------------------------------ 6. probe
step "6/7 probe + scenarios"
python3 run_experiment.py "$CONFIG" --probe
python3 run_experiment.py "$CONFIG" --scenario list

# ------------------------------------------------------------------ 7. matrix
step "7/7 matrix -> $OUT   (scenarios: $SCENARIOS | policies: $POLICIES | etas: $ETAS | seeds: $SEEDS | T=$T)"
echo "   Ctrl+C any time; the same command resumes."
SCENARIOS="$SCENARIOS" POLICIES="$POLICIES" ETAS="$ETAS" SEEDS="$SEEDS" OUT="$OUT" scripts/run_matrix.sh "$CONFIG" "$T"
echo
echo "==== done. Results: $OUT/summary.csv, summary_agg.csv, pareto.png, pareto_eta.png"
