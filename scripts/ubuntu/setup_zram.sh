#!/usr/bin/env bash
# Compressed swap (zram) so that `memory.high` has somewhere to push anonymous pages of shrunk apps.
#
#     sudo scripts/ubuntu/setup_zram.sh [SIZE_MB=4096] [ALGO=zstd|lz4|lzo-rle]
#     sudo scripts/ubuntu/setup_zram.sh --off
#
# Ubuntu 22.04 desktop ships no zram by default (only a 2 GB /swapfile on disk).  zram keeps the
# experiment's "flash writes" at zero and models the DRAM / ZRAM two-tier memory of the study; a disk
# swapfile can be kept as a lower-priority third tier.  Persistent alternative: apt install
# systemd-zram-generator and put [zram0] zram-size = 4096 compression-algorithm = zstd in
# /etc/systemd/zram-generator.conf.
set -euo pipefail
if [ "$(id -u)" != 0 ]; then echo "run with sudo" >&2; exit 1; fi

# The zram "reset" / "disksize" knobs (used by both `zramctl --reset` and `zramctl --find --size`) return
# EBUSY while anything holds the block device open.  Right after modprobe or swapoff that is usually udev /
# udisks probing the device for a moment, so settle udev and retry before giving up; on a real holder
# (a swap unit from systemd-zram-generator / zram-tools, a mount) print who it is.
zram_try() {
  local i out
  for i in 1 2 3 4 5 6 7 8 9 10; do
    if out="$("$@" 2>&1)"; then [ -n "$out" ] && printf '%s\n' "$out"; return 0; fi
    case "$out" in
      *busy*|*Busy*) udevadm settle 2>/dev/null || true; sleep 1 ;;
      *) echo "$out" >&2; return 1 ;;
    esac
  done
  echo "$out" >&2
  for d in /dev/zram*; do
    [ -e "$d" ] || continue
    echo "   $d: disksize=$(cat /sys/block/${d#/dev/}/disksize 2>/dev/null) swap=$(grep -c "^$d " /proc/swaps) mount=$(findmnt -nro TARGET -S "$d" 2>/dev/null || true)" >&2
    fuser -v "$d" 2>&1 | sed 's/^/   /' >&2 || true
  done
  echo "   holders: systemctl list-units --all '*zram*' 'dev-zram*.swap'  (zram-tools / systemd-zram-generator? stop or remove them, see manual §10)" >&2
  return 1
}

if [ "${1:-}" = "--off" ]; then
  udevadm settle 2>/dev/null || true
  for d in /dev/zram*; do
    [ -e "$d" ] || continue
    if findmnt -nro TARGET -S "$d" >/dev/null 2>&1; then echo "skip $d: mounted at $(findmnt -nro TARGET -S "$d")"; continue; fi
    grep -q "^$d " /proc/swaps && { swapoff "$d" || { echo "!! swapoff $d failed (not enough free RAM to take the pages back?)" >&2; continue; }; }
    [ "$(cat /sys/block/${d#/dev/}/disksize 2>/dev/null || echo 0)" != 0 ] || continue
    zram_try zramctl --reset "$d" && echo "reset $d"
  done
  exit 0
fi
SIZE_MB="${1:-4096}"
ALGO="${2:-zstd}"
modprobe zram 2>/dev/null || true
udevadm settle 2>/dev/null || true
if grep -q zram /proc/swaps; then
  echo "== zram already active:"; grep zram /proc/swaps; zramctl; exit 0
fi
dev="$(zram_try zramctl --find --size "${SIZE_MB}M" --algorithm "$ALGO")"
mkswap "$dev" >/dev/null
swapon -p 100 "$dev"
echo "== $dev: ${SIZE_MB} MB $ALGO, priority 100 (above the disk swapfile)"
zramctl
cat /proc/swaps
echo "   mm_stat (orig compr used ...): $(cat /sys/block/${dev#/dev/}/mm_stat)"
