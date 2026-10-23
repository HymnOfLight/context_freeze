#!/usr/bin/env bash
# One account, every app: start each app into its cgroup, walk through its login with the shared
# experiment identity (configs/accounts.json), ask for the one preparation step the work script
# needs (a chat with a focused input box, a blank document, a feed), then rehearse the scripted
# user actions once and record ready / not-ready in results/onboard/state.json.
#     scripts/ubuntu/cf_onboard.sh                 # every app of the config (installed desktop builds / mobile builds)
#     scripts/ubuntu/cf_onboard.sh wechat qq       # only these (re-run the ones that were not ready)
#     scripts/ubuntu/cf_onboard.sh --check         # status table, exit 1 when something is not ready (no prompts)
# Must be run from a shell that went through setup_cgroups.sh (or `source scripts/ubuntu/cf_enter.sh`),
# in the graphical X11 session (the login dialogs appear on this desktop).
set -euo pipefail
cd "$(dirname "$0")/../.."
CONFIG="${CONFIG:-configs/ubuntu_cn_desktop.json}"
case "${1:-}" in
  --check) shift; exec python3 run_linux.py "$CONFIG" --onboard-check ${1:+--onboard "$@"} ;;
  -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
esac
if ! grep -q '^0::/cf/' /proc/self/cgroup; then
  echo "!! this shell is not under /cf: run  source scripts/ubuntu/cf_enter.sh  first" >&2
  exit 1
fi
exec python3 run_linux.py "$CONFIG" --onboard "$@"
