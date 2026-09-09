#!/usr/bin/env bash
# Shared environment detection for macOS (Apple Silicon) / Linux hosts. Source me.

detect_sdk() {
  if [ -n "${ANDROID_HOME:-}" ] && [ -d "$ANDROID_HOME" ]; then :;
  elif [ -n "${ANDROID_SDK_ROOT:-}" ] && [ -d "$ANDROID_SDK_ROOT" ]; then export ANDROID_HOME="$ANDROID_SDK_ROOT";
  elif [ -d "$HOME/Library/Android/sdk" ]; then export ANDROID_HOME="$HOME/Library/Android/sdk";
  elif [ -d "$HOME/Android/Sdk" ]; then export ANDROID_HOME="$HOME/Android/Sdk";
  else export ANDROID_HOME="$HOME/Library/Android/sdk"; fi
  export ANDROID_SDK_ROOT="$ANDROID_HOME"

  # sdkmanager: SDK-local cmdline-tools first, then Homebrew cask
  if [ -x "$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager" ]; then
    SDKMANAGER="$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager"
    AVDMANAGER="$ANDROID_HOME/cmdline-tools/latest/bin/avdmanager"
  elif [ -x /opt/homebrew/share/android-commandlinetools/cmdline-tools/latest/bin/sdkmanager ]; then
    SDKMANAGER=/opt/homebrew/share/android-commandlinetools/cmdline-tools/latest/bin/sdkmanager
    AVDMANAGER=/opt/homebrew/share/android-commandlinetools/cmdline-tools/latest/bin/avdmanager
  elif command -v sdkmanager >/dev/null 2>&1; then
    SDKMANAGER=$(command -v sdkmanager); AVDMANAGER=$(command -v avdmanager)
  else
    SDKMANAGER=""; AVDMANAGER=""
  fi
  export SDKMANAGER AVDMANAGER
  export ADB="$ANDROID_HOME/platform-tools/adb"
  export EMULATOR="$ANDROID_HOME/emulator/emulator"
  export PATH="$ANDROID_HOME/platform-tools:$ANDROID_HOME/emulator:$PATH"
}

host_arch() {
  case "$(uname -m)" in
    arm64|aarch64) echo arm64-v8a ;;
    x86_64) echo x86_64 ;;
    *) echo arm64-v8a ;;
  esac
}

detect_sdk
