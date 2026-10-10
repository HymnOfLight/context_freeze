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
if [ "${1:-}" = "--off" ]; then
  for d in /dev/zram*; do [ -e "$d" ] && swapoff "$d" 2>/dev/null && zramctl --reset "$d" && echo "reset $d"; done
  exit 0
fi
SIZE_MB="${1:-4096}"
ALGO="${2:-zstd}"
modprobe zram 2>/dev/null || true
if grep -q zram /proc/swaps; then
  echo "== zram already active:"; grep zram /proc/swaps; zramctl; exit 0
fi
dev="$(zramctl --find --size "${SIZE_MB}M" --algorithm "$ALGO")"
mkswap "$dev" >/dev/null
swapon -p 100 "$dev"
echo "== $dev: ${SIZE_MB} MB $ALGO, priority 100 (above the disk swapfile)"
zramctl
cat /proc/swaps
echo "   mm_stat (orig compr used ...): $(cat /sys/block/${dev#/dev/}/mm_stat)"
