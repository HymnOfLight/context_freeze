#!/usr/bin/env bash
# One command from a freshly booted Ubuntu desktop to report.md: per-boot setup, environment check,
# probe / catalogue snapshots, smoke test, the experiment matrix, analysis (tables, per-app table,
# plots, Markdown report).  Every stage is idempotent; interrupt any time and re-run the same command
# to continue (finished matrix cells are skipped, an unfinished cell resumes from its checkpoint).
#
#   scripts/ubuntu/run_all.sh                                  # default matrix (~30 cells, ~8 h) into results/ubuntu_all
#   PRESET=night scripts/ubuntu/run_all.sh                     # ~50 cells, ~10 h (T=40): the usual overnight run
#   PRESET=full  scripts/ubuntu/run_all.sh                     # ~350 cells, several days; resumable, run it in tmux
#   PRESET=quick scripts/ubuntu/run_all.sh                     # smoke test only (office, none + static, T=15)
#   PLACEHOLDERS=1 PRESET=night scripts/ubuntu/run_all.sh      # apps not installed yet run as the bundled placeholder
#   WORK=1 PRESET=night scripts/ubuntu/run_all.sh              # scripted work on the foreground app, content re-checked on return (work_state_ok)
#   OUT=results/run3 T=60 CONFIG=configs/ubuntu_cn_desktop.json scripts/ubuntu/run_all.sh
#   SKIP_SMOKE=1 scripts/ubuntu/run_all.sh                     # smoke already passed; go straight to the matrix
#   ZRAM_MB=0 scripts/ubuntu/run_all.sh                        # do not touch swap (default: create 4 GB zram if none)
#   EXTRA="--sample-every 1" scripts/ubuntu/run_all.sh         # extra run_linux.py flags for every cell
#   MEM_FRAC=0.3 ETAS="0.4 0.6" scripts/ubuntu/run_all.sh      # second pass with a smaller memory.high (lower feasible eta)
#
# Run it from a terminal INSIDE the Xorg desktop session (or from ssh with DISPLAY / XAUTHORITY /
# DBUS_SESSION_BUS_ADDRESS exported, manual §2.4).  sudo is asked for once (cgroup delegation, zram).
# Everything the analysis needs ends up in $OUT: env.txt, probe.txt, catalog.txt, matrix/*.jsonl,
# matrix/report.md, matrix/summary*.csv, matrix/per_app.csv, matrix/plots/*.png.
set -euo pipefail
cd "$(dirname "$0")/../.."
ROOT="$PWD"
CONFIG="${CONFIG:-configs/ubuntu_cn_desktop.json}"
PRESET="${PRESET:-default}"
OUT="${OUT:-results/ubuntu_all}"
ZRAM_MB="${ZRAM_MB:-4096}"
case "$PRESET" in
  quick)   T="${T:-15}" ;;
  night)   T="${T:-40}" ;;
  full)    T="${T:-60}" ;;
  default) T="${T:-60}" ;;
  *) echo "unknown PRESET=$PRESET (quick | night | full | default)" >&2; exit 2 ;;
esac
mkdir -p "$OUT"
LOG="$OUT/run_all.log"
exec > >(tee -a "$LOG") 2>&1
echo "== run_all $(date -Is): preset=$PRESET T=$T config=$CONFIG out=$OUT placeholders=${PLACEHOLDERS:-0} work=${WORK:-0}"

step() { echo; echo "==== [$1] $2"; }
done_marker() { [ -f "$OUT/.done_$1" ]; }
mark_done() { date -Is > "$OUT/.done_$1"; }

# 1. display / session ---------------------------------------------------------------------------
step 1 "desktop session"
if [ -z "${DISPLAY:-}" ]; then
  # ssh case: borrow the environment of the logged-in desktop session (manual §2.4)
  pid=$(pgrep -u "$USER" -x gnome-shell | head -1 || true)
  if [ -n "$pid" ]; then
    for v in DISPLAY XAUTHORITY DBUS_SESSION_BUS_ADDRESS XDG_SESSION_TYPE; do
      val=$(tr '\0' '\n' < "/proc/$pid/environ" | sed -n "s/^$v=//p"); [ -n "$val" ] && export "$v=$val"
    done
    echo "  imported DISPLAY=$DISPLAY from gnome-shell pid $pid"
  else
    echo "!! no DISPLAY and no gnome-shell session: log in on the VM console (Ubuntu on Xorg) first" >&2; exit 2
  fi
fi
if [ "${XDG_SESSION_TYPE:-}" != x11 ] && ! xprop -root _NET_SUPPORTING_WM_CHECK >/dev/null 2>&1; then
  echo "!! not an X11 session (XDG_SESSION_TYPE=${XDG_SESSION_TYPE:-?}); choose 'Ubuntu on Xorg' at the login screen" >&2; exit 2
fi
echo "  DISPLAY=$DISPLAY session=${XDG_SESSION_TYPE:-?} wm=$(xprop -root _NET_SUPPORTING_WM_CHECK 2>/dev/null | awk '{print $NF}')"

# 2. per-boot kernel-side setup -----------------------------------------------------------------
step 2 "cgroup delegation (/sys/fs/cgroup/cf) + this shell into cf/host"
if ! grep -q '^0::/cf/' /proc/self/cgroup; then
  sudo scripts/ubuntu/setup_cgroups.sh --pid $$
fi
grep -q '^0::/cf/' /proc/self/cgroup || { echo "!! still outside /cf" >&2; exit 2; }
echo "  this shell: $(cut -d: -f3 /proc/self/cgroup)"
if [ "$ZRAM_MB" != 0 ] && ! grep -q zram /proc/swaps; then
  step 2b "zram swap ${ZRAM_MB} MB"
  sudo scripts/ubuntu/setup_zram.sh "$ZRAM_MB"
fi
# the proxy must not shape the download apps' traffic
if [ -n "${http_proxy:-}${https_proxy:-}${all_proxy:-}" ]; then
  echo "  note: proxy variables are set (${https_proxy:-${http_proxy:-$all_proxy}}); unset them for the download"
  echo "        experiments unless the apps need it (manual §2.5)."
fi

# 3. environment check + snapshots --------------------------------------------------------------
step 3 "environment check"
scripts/ubuntu/check_env.sh | tee "$OUT/env.txt" || { echo "!! fix the FAIL lines above and re-run" >&2; exit 2; }
{ echo; echo "# $(date -Is)"; uname -a; nproc; free -m; cat /proc/swaps; cat /etc/os-release | head -3;
  python3 --version; pip3 list 2>/dev/null | grep -iE 'xlib|matplotlib|numpy' || true;
  echo "proxy: http_proxy=${http_proxy:-} https_proxy=${https_proxy:-}";
  cat /sys/fs/cgroup/cf/cgroup.subtree_control; } >> "$OUT/env.txt"
step 3b "probe / catalogue"
python3 run_linux.py "$CONFIG" --probe   | tee "$OUT/probe.txt"
python3 run_linux.py "$CONFIG" --catalog | tee "$OUT/catalog.txt"
python3 run_linux.py "$CONFIG" --scenario list > "$OUT/apps_resolved.txt" || true
if command -v waydroid >/dev/null 2>&1; then
  python3 run_linux.py "$CONFIG" --mobile-status > "$OUT/mobile_status.txt" 2>&1 || true
fi
if [ "${WORK:-0}" = 1 ]; then
  python3 run_linux.py "$CONFIG" --work-plan ${PLACEHOLDERS:+--placeholders} | tee "$OUT/work_plan.txt" || true
  command -v xclip >/dev/null 2>&1 || echo "  note: xclip missing -> the clipboard oracle is unavailable (apt install xclip)"
fi
if grep -q '^missing:' "$OUT/apps_resolved.txt" && [ "${PLACEHOLDERS:-0}" != 1 ]; then
  echo "  note: $(grep -c '^missing:' "$OUT/apps_resolved.txt") app(s) are not installed and will be skipped;"
  echo "        install them (manual §4) or run with PLACEHOLDERS=1 to keep them in the scenarios as placeholders."
fi

# 4. smoke test ------------------------------------------------------------------------------------
if [ "${SKIP_SMOKE:-0}" != 1 ] && ! done_marker smoke; then
  step 4 "smoke test (office, none + static, T=15)"
  QUICK=1 OUT="$OUT/smoke" PLACEHOLDERS="${PLACEHOLDERS:-0}" WORK="${WORK:-0}" WORK_S="${WORK_S:-}" EXTRA="${EXTRA:-}" \
    scripts/ubuntu/run_matrix.sh "$CONFIG"
  python3 - "$OUT/smoke" <<'PYEOF'
import glob, os, sys
from cf.analyze import load
from cf.linux.analyze import summarize
bad = 0
files = sorted(glob.glob(sys.argv[1] + "/*.jsonl"))
if not files: print("!! smoke test produced no result file"); sys.exit(1)
for f in files:
    s = summarize(load(f))
    if not s.get("finished", True): print(f"!! {os.path.basename(f)}: unfinished"); bad = 1; continue
    lost, cold, oom = s.get("state_lost_events", 0), s.get("n_cold", 0), s.get("oom_kills", 0)
    wchk, wfail = s.get("work_checks", 0), s.get("work_fail", 0)
    print(f"  {os.path.basename(f):40s} state_lost={lost} oom={oom} n_cold={cold} work={wchk}/{wfail} "
          f"lat_p50={s.get('lat_p50_ms')} ms  bg_pss={s.get('bg_pss_avg_mb')} MB")
    if lost or cold or oom or wfail: bad = 1
if bad: print("!! smoke test invariants violated (state_lost / oom / COLD / work_fail must be 0) - see manual §7 before running the matrix")
sys.exit(bad)
PYEOF
  mark_done smoke
fi
[ "$PRESET" = quick ] && { echo "== quick preset: smoke test only. Results in $OUT/smoke"; exit 0; }

# 5. the matrix --------------------------------------------------------------------------------------
step 5 "matrix ($PRESET, T=$T) -> $OUT/matrix"
case "$PRESET" in
  night) export NIGHT=1 ;;
  full)  export FULL=1 ;;
esac
export PLACEHOLDERS="${PLACEHOLDERS:-0}" WORK="${WORK:-0}" WORK_S="${WORK_S:-}" EXTRA="${EXTRA:-}" MEM_FRAC="${MEM_FRAC:-}"
OUT="$OUT/matrix" scripts/ubuntu/run_matrix.sh "$CONFIG" "$T"

# 6. done -----------------------------------------------------------------------------------------------
step 6 "results"
ls -1 "$OUT/matrix" | grep -vE '\.(jsonl|log|ckpt)$' | sed 's/^/  /'
echo "== report: $OUT/matrix/report.md   plots: $OUT/matrix/plots/   tables: $OUT/matrix/summary*.csv, per_app.csv"
echo "== bundle for the thesis / sharing:  tar czf ${OUT##*/}_$(date +%Y%m%d).tgz $OUT"
