#!/usr/bin/env bash
# Move the CURRENT shell into /sys/fs/cgroup/cf/host (source it, do not execute it):
#     source scripts/ubuntu/cf_enter.sh
# Equivalent to `sudo scripts/ubuntu/setup_cgroups.sh` but makes the pid explicit, which also works
# from tmux / screen / VS Code terminals where the parent-of-sudo heuristic may pick the wrong process.
_cf_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
sudo "$_cf_dir/setup_cgroups.sh" --pid $$
unset _cf_dir
grep -q '^0::/cf/' /proc/self/cgroup && echo "== this shell is now in $(cut -d: -f3 /proc/self/cgroup)" \
  || echo "!! still outside /cf: $(cat /proc/self/cgroup)"
