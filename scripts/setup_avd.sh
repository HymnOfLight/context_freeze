#!/usr/bin/env bash
# Create a rootable Android Virtual Device for the freeze/swap experiments.
#
#   scripts/setup_avd.sh [API_LEVEL=35] [AVD_NAME=cf_api35] [RAM_MB=3072] [TAG=google_apis]
#
# Notes for MacBook (Apple Silicon, 16 GB):
#   * use arm64-v8a images (native under Hypervisor.framework; x86 images are unusably slow)
#   * use google_apis or default (AOSP) images -> `adb root` works.  google_apis_playstore does NOT.
#   * 16 GB host: 3072 MB guest is a comfortable default; 2048 MB creates strong memory pressure.
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/env.sh

API="${1:-35}"
AVD="${2:-cf_api${API}}"
RAM="${3:-3072}"
TAG="${4:-google_apis}"
ARCH="$(host_arch)"
IMG="system-images;android-${API};${TAG};${ARCH}"

if [ -z "$SDKMANAGER" ]; then
  cat <<EOF
sdkmanager not found. Install one of:
  1) Android Studio (then Settings > SDK Manager > SDK Tools > "Android SDK Command-line Tools"), or
  2) brew install --cask android-commandlinetools
and re-run. Expected SDK root: $ANDROID_HOME
EOF
  exit 1
fi

echo "== SDK root: $ANDROID_HOME"
echo "== installing platform-tools, emulator, $IMG"
yes | "$SDKMANAGER" --licenses >/dev/null 2>&1 || true
"$SDKMANAGER" --install "platform-tools" "emulator" "platforms;android-${API}" "$IMG"

if "$AVDMANAGER" list avd | grep -q "Name: ${AVD}$"; then
  echo "== AVD $AVD already exists"
else
  echo "== creating AVD $AVD"
  echo no | "$AVDMANAGER" create avd -n "$AVD" -k "$IMG" -d "pixel_6" --force
fi

INI="$HOME/.android/avd/${AVD}.avd/config.ini"
set_ini() { # key value
  if grep -q "^$1=" "$INI"; then
    sed -i.bak "s|^$1=.*|$1=$2|" "$INI"
  else
    echo "$1=$2" >> "$INI"
  fi
}
set_ini hw.ramSize "$RAM"
set_ini hw.cpu.ncore 4
set_ini disk.dataPartition.size 8G
set_ini hw.gpu.enabled yes
set_ini hw.gpu.mode auto
set_ini hw.keyboard yes
set_ini fastboot.forceColdBoot yes
rm -f "$INI.bak"

echo "== AVD $AVD ready (RAM ${RAM} MB, image $IMG)"
echo "   start it with: scripts/start_emulator.sh $AVD $RAM"
