#!/usr/bin/env bash
# One-time manual pass over the installed mainland-China apps before the experiments.
#
# Every one of these apps shows a privacy agreement (个人信息保护指引) on first start, most also
# a login / phone-number screen, some a "check for update" dialog. am start -W measures the time to
# the first frame of whatever Activity comes up, so an un-accepted agreement or a login screen would
# be measured instead of the real app. This script launches each app in turn and waits for you to
# handle the dialogs in the emulator window; press Enter when the app shows its normal home screen.
#
#   scripts/first_run_cn_apps.sh [CONFIG=configs/cn_apps.json]
#
# Tips
#   * 微信 / QQ need a logged-in account; use a secondary account - logging in from an emulator can
#     trigger risk control. 微博 / 网易云音乐 / 哔哩哔哩 / 抖音 / 淘宝 / 京东 browse without login.
#   * Turn off in-app "自启动/后台保活" prompts and notification permission requests when asked:
#     background wake-ups of frozen apps are exactly what produces `Sync transaction while frozen`
#     kills later; the runner records them via exit-info either way.
#   * The script also disables the apps' battery-optimisation exemptions request and puts them in
#     the ACTIVE standby bucket so Android itself does not restrict them differently from each other.
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/env.sh
CONFIG="${1:-configs/cn_apps.json}"

PKGS=$(python3 - "$CONFIG" <<'EOF'
import json, sys
cfg = json.load(open(sys.argv[1]))
print("\n".join(cfg["apps"]))
EOF
)
INSTALLED="$("$ADB" shell pm list packages | tr -d '\r')"

names() { python3 -c "import sys; sys.path.insert(0,'.'); from cf.scenarios import display_name; print(display_name('$1'))"; }

# pkg -> "pkg/Activity" of the LAUNCHER activity, the same way cf/device.py does it.
# `am start -a MAIN -c LAUNCHER <pkg>` (implicit intent) only resolves when the launcher
# <intent-filter> also declares android.intent.category.DEFAULT; the CN apps (and every app built
# from the Android Studio template) do not, so that form fails with "unable to resolve Intent"
# although the app is installed fine. Resolve the component first and start it with -n.
launcher_activity() {
  "$ADB" shell "cmd package resolve-activity --brief -a android.intent.action.MAIN -c android.intent.category.LAUNCHER $1" \
    | tr -d '\r' | awk -v pkg="$1" '{ gsub(/^[ \t]+|[ \t]+$/, ""); if (index($0, pkg "/") == 1 && $0 !~ / /) comp = $0 } END { print comp }'
}

start_app() {   # start_app <pkg> <component>: prints LaunchState / TotalTime / Error lines
  local pkg="$1" comp="$2"
  if [ -n "$comp" ]; then
    "$ADB" shell "am start -W -a android.intent.action.MAIN -c android.intent.category.LAUNCHER -n $comp" \
      | tr -d '\r' | grep -E "LaunchState|TotalTime|Error" || true
  else
    # no resolvable launcher activity: let monkey pick one (it does not need CATEGORY_DEFAULT either)
    "$ADB" shell "monkey -p $pkg -c android.intent.category.LAUNCHER 1" >/dev/null 2>&1 \
      && echo "   started via monkey (no LaunchState/TotalTime available)" \
      || echo "   Error: could not start $pkg"
  fi
}

diagnose() {   # why is the installed package not launchable?
  local pkg="$1"
  echo "   !! no LAUNCHER activity resolves for $pkg. Diagnostics:"
  "$ADB" shell "pm path $pkg" | tr -d '\r' | sed 's/^/      /'
  "$ADB" shell "dumpsys package $pkg" | tr -d '\r' \
    | grep -E "installed=|enabled=|hidden=|suspended=|stopped=|versionName=|primaryCpuAbi=" | sed 's/^/      /' | head -8
  echo "      activities with MAIN/LAUNCHER (cmd package query-activities):"
  local acts
  acts=$("$ADB" shell "cmd package query-activities --brief -a android.intent.action.MAIN -c android.intent.category.LAUNCHER" \
    | tr -d '\r' | grep -F "$pkg/" || true)
  echo "${acts:-(none)}" | sed 's/^[[:space:]]*/      /'
  echo "      -> if 'installed=false' / 'enabled=2|3': pm install -r -g the APK again;"
  echo "         if 'primaryCpuAbi=armeabi-v7a' on an arm64-only image: the build is 32-bit, re-download (install_cn_apps.py --download)"
}

echo "== first-run pass; the emulator window must be visible. Press Enter after each app is on its home screen."
for pkg in $PKGS; do
  if ! grep -q "package:$pkg\$" <<<"$INSTALLED"; then
    echo "-- $pkg not installed, skipped"
    continue
  fi
  name=$(names "$pkg")
  echo
  echo "== $name ($pkg)"
  "$ADB" shell am set-standby-bucket "$pkg" active >/dev/null 2>&1 || true
  "$ADB" shell cmd appops set "$pkg" RUN_IN_BACKGROUND allow >/dev/null 2>&1 || true
  comp=$(launcher_activity "$pkg")
  if [ -n "$comp" ]; then
    echo "   launcher activity: $comp"
  else
    diagnose "$pkg"
  fi
  start_app "$pkg" "$comp"
  read -r -p "   accept the agreement / log in / dismiss update prompts, then press Enter (s = skip, q = quit): " ans
  case "$ans" in
    q) break;;
    s) ;;
    *)
      # second start is what the experiment will see: it must be fast and land on the main Activity
      "$ADB" shell am force-stop "$pkg"; sleep 1
      start_app "$pkg" "$comp" | grep -E "LaunchState|TotalTime|Error|monkey" | tr '\n' ' '; echo
      ;;
  esac
  "$ADB" shell input keyevent KEYCODE_HOME
done
echo
echo "== done. Check with:  python3 run_experiment.py $CONFIG --probe"
echo "   and list the scenarios usable with the installed apps:  python3 run_experiment.py $CONFIG --scenario list"
