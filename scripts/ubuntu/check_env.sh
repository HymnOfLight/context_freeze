#!/usr/bin/env bash
# Quick environment check inside the Ubuntu VM (no python deps needed). Exit code 0 = ready.
cd "$(dirname "$0")/../.."
ok=0; bad=0
pass() { echo "  ok   $1"; ok=$((ok+1)); }
fail() { echo "  FAIL $1"; bad=$((bad+1)); }
warn() { echo "  warn $1"; }
echo "== context_freeze / Ubuntu throttling back-end: environment check"
. /etc/os-release 2>/dev/null; echo "  host $PRETTY_NAME, kernel $(uname -r), $(uname -m), $(nproc) cpu, $(awk '/MemTotal/{printf "%d MB", $2/1024}' /proc/meminfo) RAM"
[ "${XDG_SESSION_TYPE:-}" = x11 ] && pass "X11 session" || fail "session type is '${XDG_SESSION_TYPE:-?}' (need Xorg: gear icon at the login screen -> 'Ubuntu on Xorg')"
[ -n "${DISPLAY:-}" ] && pass "DISPLAY=$DISPLAY" || fail "no DISPLAY (open a terminal inside the desktop session)"
for t in xdotool xprop python3 dbus-monitor zramctl; do command -v $t >/dev/null && pass "$t" || fail "$t missing (scripts/ubuntu/install_deps.sh)"; done
python3 -c "import Xlib" 2>/dev/null && pass "python3-xlib" || warn "python3-xlib missing: no _NET_WM_PING latency"
command -v xclip >/dev/null && pass "xclip" || warn "xclip missing: --work has no clipboard oracle (apt install xclip)"
grep -qs "cgroup2 /sys/fs/cgroup" /proc/mounts && pass "cgroup v2 unified" || fail "cgroup v2 not mounted at /sys/fs/cgroup"
if [ -d /sys/fs/cgroup/cf ]; then
  [ -w /sys/fs/cgroup/cf/cgroup.procs ] && pass "/sys/fs/cgroup/cf writable [$(cat /sys/fs/cgroup/cf/cgroup.subtree_control)]" || fail "/sys/fs/cgroup/cf not writable by $(id -un): sudo scripts/ubuntu/setup_cgroups.sh"
  grep -q '^0::/cf/' /proc/self/cgroup && pass "this shell is in $(cut -d: -f3 /proc/self/cgroup)" || fail "this shell is outside /cf ($(cut -d: -f3 /proc/self/cgroup)): source scripts/ubuntu/cf_enter.sh"
else
  fail "/sys/fs/cgroup/cf missing: sudo scripts/ubuntu/setup_cgroups.sh"
fi
[ -e /sys/fs/cgroup/memory.reclaim ] && pass "memory.reclaim (proactive shrink)" || warn "no memory.reclaim (kernel < 5.19): sudo scripts/ubuntu/install_deps.sh --hwe"
if grep -q zram /proc/swaps; then pass "zram swap: $(grep zram /proc/swaps | awk '{printf "%d MB", $3/1024}')"; elif [ "$(awk 'NR>1' /proc/swaps | wc -l)" -gt 0 ]; then warn "only disk swap ($(awk 'NR>1{printf "%s ", $1}' /proc/swaps)) - sudo scripts/ubuntu/setup_zram.sh 4096"; else fail "no swap at all: sudo scripts/ubuntu/setup_zram.sh 4096"; fi
snap list 2>/dev/null | grep -qE '^(firefox|chromium) ' && warn "snap browsers installed: their processes escape our cgroups; install the deb versions (manual §4)"
echo "== $ok ok, $bad problems"
[ $bad -eq 0 ]
