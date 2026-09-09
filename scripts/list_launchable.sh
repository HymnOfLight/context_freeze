#!/usr/bin/env bash
# Print every package that has a LAUNCHER activity, so configs/*.json can be adjusted.
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/env.sh
"$ADB" shell "cmd package query-activities --brief -a android.intent.action.MAIN -c android.intent.category.LAUNCHER" \
  | tr -d '\r' | grep '/' | sed -E 's#^[[:space:]]+##; s#/.*##' | sort -u
