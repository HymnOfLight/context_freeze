#!/usr/bin/env bash
# Start apps into their cgroups (/sys/fs/cgroup/cf/<id>) so you can log in / accept dialogs once;
# the runner later only switches between their windows and never restarts them.
#     scripts/ubuntu/cf_launch.sh wechat qq dingtalk
# Must be run from a shell that went through setup_cgroups.sh (or `source scripts/ubuntu/cf_enter.sh`).
set -euo pipefail
cd "$(dirname "$0")/../.."
if ! grep -q '^0::/cf/' /proc/self/cgroup; then
  echo "!! this shell is not under /cf: run  source scripts/ubuntu/cf_enter.sh  first" >&2
  exit 1
fi
CONFIG="${CONFIG:-configs/ubuntu_cn_desktop.json}"
exec python3 run_linux.py "$CONFIG" --launch "$@"
