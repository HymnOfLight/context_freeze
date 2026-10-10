#!/usr/bin/env bash
# Root helper: move processes that live outside /cf (snap scopes, D-Bus-activated apps, apps started
# before setup_cgroups.sh) into an app leaf.  Used by the runner when `sudo_adopt` is true; to make
# that non-interactive add to /etc/sudoers.d/cf (visudo -f):
#     <user> ALL=(root) NOPASSWD: /home/<user>/context_freeze/scripts/ubuntu/cf_adopt.sh
#
#     sudo scripts/ubuntu/cf_adopt.sh /sys/fs/cgroup/cf/wechat 1234 1235
set -euo pipefail
ROOT="${CF_CGROUP_ROOT:-/sys/fs/cgroup}"
DIR="${CF_CGROUP_DIR:-cf}"
dest="$1"; shift
case "$dest" in
  "$ROOT/$DIR"/*) ;;
  *) echo "refusing: $dest is not under $ROOT/$DIR" >&2; exit 2 ;;
esac
[ -d "$dest" ] || mkdir -p "$dest"
rc=0
for pid in "$@"; do
  if echo "$pid" > "$dest/cgroup.procs" 2>/tmp/cf_adopt.err; then
    echo "moved $pid -> $dest"
  else
    echo "failed $pid: $(cat /tmp/cf_adopt.err)" >&2; rc=1
  fi
done
exit $rc
