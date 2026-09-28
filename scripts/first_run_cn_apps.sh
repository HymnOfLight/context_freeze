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
  "$ADB" shell "am start -W -a android.intent.action.MAIN -c android.intent.category.LAUNCHER $pkg" | tr -d '\r' | grep -E "LaunchState|TotalTime|Error" || true
  read -r -p "   accept the agreement / log in / dismiss update prompts, then press Enter (s = skip, q = quit): " ans
  case "$ans" in
    q) break;;
    s) ;;
    *)
      # second start is what the experiment will see: it must be fast and land on the main Activity
      "$ADB" shell am force-stop "$pkg"; sleep 1
      "$ADB" shell "am start -W -a android.intent.action.MAIN -c android.intent.category.LAUNCHER $pkg" | tr -d '\r' \
        | grep -E "LaunchState|TotalTime" | tr '\n' ' '; echo
      ;;
  esac
  "$ADB" shell input keyevent KEYCODE_HOME
done
echo
echo "== done. Check with:  python3 run_experiment.py $CONFIG --probe"
echo "   and list the scenarios usable with the installed apps:  python3 run_experiment.py $CONFIG --scenario list"
