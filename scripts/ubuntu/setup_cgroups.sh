#!/usr/bin/env bash
# One-time (per boot) cgroup v2 setup for the throttling experiments. Run it WITH sudo FROM THE SHELL
# you will start the experiments in:
#
#     sudo scripts/ubuntu/setup_cgroups.sh            # creates /sys/fs/cgroup/cf, delegates it to you,
#                                                      # moves this shell into /sys/fs/cgroup/cf/host
#     sudo scripts/ubuntu/setup_cgroups.sh --pid $$    # same, explicit shell pid (used by cf_enter.sh)
#     sudo scripts/ubuntu/setup_cgroups.sh --no-enter  # only create / delegate
#
# Why the shell has to move: cgroup v2 lets an unprivileged user move a process only when it can write
# the destination's cgroup.procs AND the cgroup.procs of the nearest common ancestor of source and
# destination.  Apps started from a shell living in /cf/host can therefore be placed into /cf/<app>
# by the user (ancestor = /cf, owned by the user); apps started from a normal terminal (user.slice)
# cannot (ancestor = root).  Nothing else needs root afterwards.
#
# The tree is not persistent: re-run after every reboot (and in every new terminal you want to use).
set -euo pipefail

ROOT="${CF_CGROUP_ROOT:-/sys/fs/cgroup}"
DIR="${CF_CGROUP_DIR:-cf}"
BASE="$ROOT/$DIR"
ENTER=1
PID=""
while [ $# -gt 0 ]; do
  case "$1" in
    --no-enter) ENTER=0 ;;
    --pid) PID="$2"; shift ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown argument $1" >&2; exit 2 ;;
  esac
  shift
done

if [ "$(id -u)" != 0 ]; then
  echo "run with sudo: sudo $0 $*" >&2; exit 1
fi
TARGET_USER="${SUDO_USER:-}"
if [ -z "$TARGET_USER" ] || [ "$TARGET_USER" = root ]; then
  echo "!! started as root directly; the desktop user must own $BASE. Run: sudo $0 from your user session" >&2
  exit 1
fi

if ! grep -qs "cgroup2 $ROOT" /proc/mounts && ! grep -qs " $ROOT cgroup2" /proc/mounts; then
  echo "!! $ROOT is not a cgroup2 mount. Ubuntu 22.04 boots with the unified hierarchy by default;" >&2
  echo "   remove systemd.unified_cgroup_hierarchy=0 / cgroup_no_v1 from the kernel cmdline if present." >&2
  exit 1
fi

# controllers for our subtree: must be enabled in the root's subtree_control first
avail="$(cat "$ROOT/cgroup.controllers")"
for c in cpu cpuset memory io; do
  if echo " $avail " | grep -q " $c "; then
    echo "+$c" > "$ROOT/cgroup.subtree_control" 2>/dev/null || echo "warn: could not enable $c at the root (systemd may own it; usually fine)"
  else
    echo "warn: controller $c not available in this kernel ($avail)"
  fi
done

mkdir -p "$BASE/host"
sub="$(cat "$BASE/cgroup.controllers")"
for c in cpu cpuset memory io; do
  echo " $sub " | grep -q " $c " && { echo "+$c" > "$BASE/cgroup.subtree_control" || echo "warn: +$c failed in $BASE"; }
done

# delegation: the user may create leaves, move processes between them and write the knobs
chown -R "$TARGET_USER": "$BASE"
chmod 755 "$BASE"

if [ "$ENTER" = 1 ]; then
  if [ -z "$PID" ]; then
    # $PPID is sudo; its parent is the interactive shell
    PID="$(ps -o ppid= -p "$PPID" | tr -d ' ')"
  fi
  if [ -n "$PID" ] && [ -d "/proc/$PID" ]; then
    echo "$PID" > "$BASE/host/cgroup.procs"
    echo "== moved shell pid $PID ($(tr -d '\0' < "/proc/$PID/comm")) into $BASE/host"
  else
    echo "warn: could not determine the shell pid; use --pid \$\$" >&2
  fi
fi

echo "== $BASE ready: controllers [$(cat "$BASE/cgroup.subtree_control")], owner $TARGET_USER"
echo "   effective cpus: $(cat "$BASE/cpuset.cpus.effective" 2>/dev/null || cat "$ROOT/cpuset.cpus.effective")"
if [ -e "$BASE/memory.reclaim" ]; then echo "   memory.reclaim: yes (proactive shrink)"; else
  echo "   memory.reclaim: NO (kernel < 5.19) - install linux-generic-hwe-22.04 for proactive reclaim"; fi
echo "   start experiments from this shell:  python3 run_linux.py configs/ubuntu_cn_desktop.json --probe"
