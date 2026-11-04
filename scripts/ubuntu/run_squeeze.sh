#!/usr/bin/env bash
# Experimental "squeeze" run: per-process accounting of the memory an app holds in the foreground vs
# after it was switched away, aggressive reclaim + CPU limits on the background apps, and the steady
# swap-out rate while they sit quiet.  One scenario (office), 5 apps, 3 cells - NOT the full matrix.
#
#   scripts/ubuntu/run_squeeze.sh                      # none baseline + squeeze + mild reference, T=12, ~1.5 h
#   CELLS="none squeeze" scripts/ubuntu/run_squeeze.sh  # subset: none | squeeze | mild
#   T=20 DWELL=240 scripts/ubuntu/run_squeeze.sh        # longer quiet windows
#   SEEDS="1 2" scripts/ubuntu/run_squeeze.sh           # replicates
#   ONBOARD=1 scripts/ubuntu/run_squeeze.sh             # walk the 5 apps through login / prep first
#   SWAPPINESS=60 scripts/ubuntu/run_squeeze.sh         # leave vm.swappiness alone (default: 200 for the run, restored at exit)
#   STRICT=0 scripts/ubuntu/run_squeeze.sh              # go on even when apps outside the experiment are still running
#   DRY=1 scripts/ubuntu/run_squeeze.sh                 # print the commands only
#
# Before it starts it checks that nothing but the 5 experiment apps is running under /cf (the other
# apps of the night runs - iqiyi, netease_music, xunlei ... - would use RAM / CPU and pollute the
# system-wide swap-out rate; quit them, or STRICT=0) and that the zram store is not already full.
#
# cells (configs/ubuntu_arm64_squeeze.json):
#   none     policy none: nothing limited - what the apps hold on their own
#   squeeze  static + the aggressive profile of the config: cpuset last core + cpu.max 10 % of it + cpu.weight 1,
#            memory.high = 0.1 x m_fg (>= 64 MB), memory.reclaim x3 on switch-away and every 30 s, no thrash guard
#   mild     static with the night-run profile for reference: 1 core quota, memory.high = 0.5 x m_fg, one reclaim
#
# Output: $OUT/*.jsonl (+ .ckpt while running; re-run the same command to resume), summary.csv,
# per_app.csv, per_process.csv, squeeze_apps.csv (per app: m_fg, background resident, CPU % of a core,
# swap traffic per minute, switch-back latency), squeeze_tables.xlsx (the same tables with Chinese headers,
# needs openpyxl), plots/ (incl. squeeze_apps.png), report.md.  Run it inside the Xorg session (or with DISPLAY
# exported, manual §2.4) after setup_cgroups.sh.
set -euo pipefail
cd "$(dirname "$0")/../.."
CONFIG="${CONFIG:-configs/ubuntu_arm64_squeeze.json}"
OUT="${OUT:-results/arm64_squeeze}"
T="${T:-12}"
DWELL="${DWELL:-150}"
SEEDS="${SEEDS:-1}"
CELLS="${CELLS:-none squeeze mild}"
EXTRA="${EXTRA:-}"
DRY="${DRY:-0}"
mkdir -p "$OUT"
LOG="$OUT/run_squeeze.log"
exec > >(tee -a "$LOG") 2>&1
echo "== run_squeeze $(date -Is): config=$CONFIG T=$T dwell=${DWELL}s seeds=$SEEDS cells=$CELLS out=$OUT"

if [ "$DRY" != 1 ]; then
  if [ -z "${DISPLAY:-}" ]; then
    pid=$(pgrep -u "$USER" -x gnome-shell | head -1 || true)
    [ -n "$pid" ] || { echo "!! no DISPLAY and no gnome-shell session" >&2; exit 2; }
    for v in DISPLAY XAUTHORITY DBUS_SESSION_BUS_ADDRESS XDG_SESSION_TYPE; do
      val=$(tr '\0' '\n' < "/proc/$pid/environ" | sed -n "s/^$v=//p"); [ -n "$val" ] && export "$v=$val"
    done
  fi
  grep -q '^0::/cf/' /proc/self/cgroup || sudo scripts/ubuntu/setup_cgroups.sh --pid $$
  grep -q '^0::/cf/' /proc/self/cgroup || { echo "!! still outside /cf" >&2; exit 2; }
  if [ "${ZRAM_MB:-4096}" != 0 ] && ! grep -q zram /proc/swaps; then sudo scripts/ubuntu/setup_zram.sh "${ZRAM_MB:-4096}"; fi
  python3 run_linux.py "$CONFIG" --scenario list > "$OUT/apps_resolved.txt" || true
  if grep -q '^missing:' "$OUT/apps_resolved.txt"; then
    echo "!! not installed: $(grep '^missing:' "$OUT/apps_resolved.txt" | tr '\n' ' ')"; fi
  # apps outside the experiment still running under /cf (from a previous matrix) distort every system-wide number
  want=$(python3 -c "import json,sys; print(' '.join(json.load(open(sys.argv[1]))['apps']))" "$CONFIG")
  others=""
  for leaf in /sys/fs/cgroup/cf/*/; do
    id=$(basename "$leaf"); [ "$id" = host ] && continue
    n=$(wc -l < "$leaf/cgroup.procs" 2>/dev/null || echo 0); [ "$n" -gt 0 ] || continue
    case " $want " in *" $id "*) continue;; esac
    others+=" $id($n procs, $(( $(cat "$leaf/memory.current") / 1048576 )) MB)"
  done
  if [ -n "$others" ]; then
    echo "!! running but not part of this experiment:$others"
    echo "   quit them (they take RAM / CPU and add to the system swap-out rate), or STRICT=0 to run anyway"
    [ "${STRICT:-1}" = 1 ] && exit 2
  fi
  zu=$(awk '/zram/ {u+=$4; t+=$3} END {printf "%d %d", u/1024, t/1024}' /proc/swaps); zused=${zu% *}; ztot=${zu#* }
  echo "  zram: ${zused} / ${ztot} MB in use  swappiness=$(cat /proc/sys/vm/swappiness)"
  if [ "${ztot:-0}" -gt 0 ] && [ "$zused" -gt $((ztot / 2)) ]; then
    echo "  note: zram is more than half full before the run - leftovers of the previous matrix; for a clean"
    echo "        baseline quit the other apps and reset it:  sudo scripts/ubuntu/setup_zram.sh --off && sudo scripts/ubuntu/setup_zram.sh ${ZRAM_MB:-4096}"
  fi
  # vm.swappiness 200 = prefer swapping anonymous pages over dropping file cache (the zram recommendation);
  # restored when the script exits
  SWAPPINESS="${SWAPPINESS:-200}"
  old_swappiness=$(cat /proc/sys/vm/swappiness)
  if [ "$SWAPPINESS" != "$old_swappiness" ]; then
    sudo sysctl -q -w vm.swappiness="$SWAPPINESS" && trap 'sudo sysctl -q -w vm.swappiness='"$old_swappiness"' || true' EXIT
    echo "  vm.swappiness $old_swappiness -> $(cat /proc/sys/vm/swappiness) for the run"
  fi
  if [ "${ONBOARD:-0}" = 1 ]; then
    python3 -m cf.linux.blankdoc --reset >/dev/null
    CONFIG="$CONFIG" scripts/ubuntu/cf_onboard.sh || true
  fi
  python3 run_linux.py "$CONFIG" --onboard-check | tee "$OUT/onboard.txt" \
    || echo "  note: some apps are not ready - ONBOARD=1 (or scripts/ubuntu/cf_onboard.sh) first"
fi

M_FG_FROM=""
FAILED=0
run_one() { # name policy seed [extra flags...]
  local name="$1" pol="$2" seed="$3" rc; shift 3
  if [ -f "$OUT/${name}.jsonl" ] && tail -c 4096 "$OUT/${name}.jsonl" | grep -q '"type": "end"'; then
    echo "== $name already finished - skipping"; return 0
  fi
  local cmd=(python3 -u run_linux.py "$CONFIG" --policy "$pol" --eta 0.0 --T "$T" --seed "$seed" --scenario office
             --name "$name" --out-dir "$OUT" --dwell "$DWELL" --per-process --work)
  [ -n "$M_FG_FROM" ] && cmd+=(--m-fg-from "$M_FG_FROM")
  # shellcheck disable=SC2206
  [ -n "$EXTRA" ] && cmd+=($EXTRA)
  [ $# -gt 0 ] && cmd+=("$@")
  [ -f "$OUT/${name}.ckpt" ] && cmd+=(--resume)
  if [ "$DRY" = 1 ]; then echo "${cmd[*]}"; return 0; fi
  set +e
  "${cmd[@]}" 2>&1 | tee -a "$OUT/${name}.console.log"
  rc=${PIPESTATUS[0]}
  set -e
  if [ "$rc" = 130 ]; then echo "== interrupted. Resume with:  OUT=$OUT $0"; exit 130
  elif [ "$rc" = 2 ]; then echo "== preflight failed; fix the environment and re-run:  OUT=$OUT $0"; exit 2
  elif [ "$rc" != 0 ]; then echo "!! $name failed (exit $rc); continuing"; FAILED=1; fi
  if [ -z "$M_FG_FROM" ] && grep -q '"type": "budget"' "$OUT/${name}.jsonl" 2>/dev/null; then
    M_FG_FROM="$OUT/${name}.jsonl"; echo "== m_fg (per-app foreground footprint) fixed from $M_FG_FROM"
  fi
}
has() { echo " $1 " | grep -q " $2 "; }

for seed in $SEEDS; do
  has "$CELLS" none    && run_one "none_office_s${seed}" none "$seed"
  has "$CELLS" squeeze && run_one "squeeze_office_s${seed}" static "$seed"
  has "$CELLS" mild    && run_one "mild_office_s${seed}" static "$seed" \
                            --cpu-mode quota --cpu-cores 1 --cpu-weight 20 --mem-frac 0.5 --squeeze 0 --squeeze-every 0
done

[ "$DRY" = 1 ] && exit 0
if ! ls "$OUT"/*.jsonl >/dev/null 2>&1; then echo "!! no result files in $OUT"; exit 1; fi
python3 -m cf.linux.analyze "$OUT"/*.jsonl --csv "$OUT/summary.csv" --per-app "$OUT/per_app.csv" \
  --per-proc "$OUT/per_process.csv" --squeeze-apps "$OUT/squeeze_apps.csv" --plots-dir "$OUT/plots" --md "$OUT/report.md" \
  --xlsx "$OUT/squeeze_tables.xlsx" || true
python3 -m cf.linux.analyze "$OUT"/*.jsonl --squeeze-apps --per-proc 2>/dev/null | sed -n '/^per app (squeeze/,$p' || true
[ "$FAILED" = 1 ] && echo "== some cells are incomplete; finish them with:  OUT=$OUT $0"
echo "== results in $OUT  (report.md, squeeze_tables.xlsx, summary.csv, per_app.csv, per_process.csv, squeeze_apps.csv, plots/squeeze_apps.png)"
