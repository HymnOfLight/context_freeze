#!/usr/bin/env bash
# Three-class experiment (manual §6.9): one short run per cell that answers, for three kinds of apps,
# "what does limiting the background cost?"
#   1. interactive (wechat / qq / wps / firefox): how long a call / switch takes to answer and whether the
#      app is smooth right afterwards
#   2. compute (bench_numeric, bench_compress): time per job and jobs per minute, foreground vs background
#   3. download (bench_download): MB/s and TCP connect time, foreground vs background
#
#   scripts/ubuntu/run_classes.sh                        # none + mild + squeeze, 2 rounds, ~9 min per cell
#   CELLS="none squeeze" scripts/ubuntu/run_classes.sh    # subset: none | mild | squeeze  (~18 min)
#   ROUNDS=3 DWELL=20 scripts/ubuntu/run_classes.sh       # more calls per app / longer background stretches
#   SEEDS="1 2" scripts/ubuntu/run_classes.sh             # replicates (the visiting order is fixed; the seed only names the run)
#   DL_URL=https://mirrors.tuna.tsinghua.edu.cn/ubuntu-releases/22.04/ubuntu-22.04.5-desktop-amd64.iso \
#     scripts/ubuntu/run_classes.sh                       # download a real file instead of the local rate-limited source
#   DL_RATE_MBPS=10 scripts/ubuntu/run_classes.sh         # local source: MB/s per connection (default 20 = a 160 Mbit/s line)
#   RTT_HOST=223.5.5.5:443 scripts/ubuntu/run_classes.sh  # extra host for the connect-time probe ("" = none)
#   ONBOARD=1 scripts/ubuntu/run_classes.sh               # walk the 4 interactive apps through login / prep first
#   EXTRA="--placeholders" scripts/ubuntu/run_classes.sh  # interactive apps that are not installed run as the bundled stand-in
#   STRICT=0 / SWAPPINESS=60 / DRY=1                      # as in run_squeeze.sh
#
# cells (configs/ubuntu_arm64_classes.json):
#   none     policy none: nothing limited - the baseline every "x" / "%" column refers to
#   mild     static, 1 core cpu.max quota + cpu.weight 20, memory.high = 0.5 x m_fg, one reclaim
#   squeeze  static, the config's profile: all background apps on the last core, cpu.max 10 % of it, cpu.weight 1,
#            memory.high = 0.1 x m_fg, memory.reclaim x3 on the switch-away
# All cells walk the same fixed visiting order (trace kind "pattern"), so even this short run is comparable.
#
# Output: $OUT/*.jsonl (+ .ckpt while running; re-run the same command to resume), summary.csv,
# classes_interactive.csv / classes_compute.csv / classes_download.csv, classes_report.md (the three tables),
# classes_tables.xlsx (Chinese headers), plots/classes.png and plots/<run>_bench.png.
# Run it inside the Xorg session (or with DISPLAY exported, manual §2.4) after setup_cgroups.sh.
set -euo pipefail
cd "$(dirname "$0")/../.."
CONFIG="${CONFIG:-configs/ubuntu_arm64_classes.json}"
OUT="${OUT:-results/arm64_classes}"
ROUNDS="${ROUNDS:-2}"
DWELL="${DWELL:-15}"
SEEDS="${SEEDS:-1}"
CELLS="${CELLS:-none mild squeeze}"
DL_URL="${DL_URL:-}"
DL_RATE_MBPS="${DL_RATE_MBPS:-20}"
DL_PORT="${DL_PORT:-18080}"
RTT_HOST="${RTT_HOST-www.baidu.com:443}"
EXTRA="${EXTRA:-}"
DRY="${DRY:-0}"
mkdir -p "$OUT"
LOG="$OUT/run_classes.log"
exec > >(tee -a "$LOG") 2>&1
PAT=$(python3 -c "import json,sys; print(len(json.load(open(sys.argv[1]))['trace']['pattern']))" "$CONFIG")
T=$((ROUNDS * PAT))
echo "== run_classes $(date -Is): config=$CONFIG rounds=$ROUNDS (T=$T) dwell=${DWELL}s seeds=$SEEDS cells=$CELLS out=$OUT"

SRV_PID=""
old_swappiness=""
cleanup() {
  [ -n "$SRV_PID" ] && kill "$SRV_PID" 2>/dev/null || true
  # the bench apps would otherwise keep computing / downloading after the experiment
  pkill -f "cf.linux.bench_app --app" 2>/dev/null || true
  [ -n "$old_swappiness" ] && sudo sysctl -q -w vm.swappiness="$old_swappiness" || true
}

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
  # shellcheck disable=SC2086
  python3 run_linux.py "$CONFIG" --scenario list > "$OUT/apps_resolved.txt" || true
  if grep -q '^missing:' "$OUT/apps_resolved.txt"; then
    echo "!! not installed: $(grep '^missing:' "$OUT/apps_resolved.txt" | tr '\n' ' ')  (EXTRA=\"--placeholders\" runs them as the stand-in)"; fi
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
    echo "   quit them (they take RAM / CPU and distort the job times), or STRICT=0 to run anyway"
    [ "${STRICT:-1}" = 1 ] && exit 2
  fi
  trap cleanup EXIT
  # bench apps left over from an earlier run would keep their old log position and old download source
  pkill -f "cf.linux.bench_app --app" 2>/dev/null && sleep 1 || true
  SWAPPINESS="${SWAPPINESS:-200}"
  cur=$(cat /proc/sys/vm/swappiness)
  if [ "$SWAPPINESS" != "$cur" ]; then
    sudo sysctl -q -w vm.swappiness="$SWAPPINESS" && old_swappiness="$cur"
    echo "  vm.swappiness $cur -> $(cat /proc/sys/vm/swappiness) for the run"
  fi
  # the download source: a real URL, or the bundled rate-limited one on the loopback (it runs in this
  # script's cgroup /cf/host, which is never limited)
  if [ -z "$DL_URL" ]; then
    DL_URL="http://127.0.0.1:${DL_PORT}/stream"
    if ! python3 -m cf.linux.bench_app --check-url "$DL_URL" >/dev/null 2>&1; then
      python3 -m cf.linux.bench_app --serve "$DL_PORT" --rate-mbps "$DL_RATE_MBPS" >> "$OUT/download_source.log" 2>&1 &
      SRV_PID=$!
      sleep 1
    fi
    echo "  download source: $DL_URL (local, ${DL_RATE_MBPS} MB/s per connection)"
  else
    echo "  download source: $DL_URL"
  fi
  why=$(python3 -m cf.linux.bench_app --check-url "$DL_URL" 2>&1) || { echo "!! download source not reachable: $why" >&2; exit 2; }
  export CF_BENCH_URL="$DL_URL" CF_BENCH_RTT_HOST="$RTT_HOST"
  [ -n "$RTT_HOST" ] && echo "  connect-time probe also to: $RTT_HOST"
  if [ "${ONBOARD:-0}" = 1 ]; then
    python3 -m cf.linux.blankdoc --reset >/dev/null
    CONFIG="$CONFIG" scripts/ubuntu/cf_onboard.sh || true
  fi
  # shellcheck disable=SC2086
  python3 run_linux.py "$CONFIG" --onboard-check $EXTRA | tee "$OUT/onboard.txt" \
    || echo "  note: some apps are not ready - ONBOARD=1 (or scripts/ubuntu/cf_onboard.sh) first"
fi

M_FG_FROM=""
FAILED=0
run_one() { # name policy seed [extra flags...]
  local name="$1" pol="$2" seed="$3" rc; shift 3
  if [ -f "$OUT/${name}.jsonl" ] && tail -c 4096 "$OUT/${name}.jsonl" | grep -q '"type": "end"'; then
    echo "== $name already finished - skipping"
    [ -z "$M_FG_FROM" ] && M_FG_FROM="$OUT/${name}.jsonl"
    return 0
  fi
  local cmd=(python3 -u run_linux.py "$CONFIG" --policy "$pol" --eta 0.0 --T "$T" --seed "$seed"
             --name "$name" --out-dir "$OUT" --dwell "$DWELL" --work)
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
  has "$CELLS" none    && run_one "none_classes_s${seed}" none "$seed"
  has "$CELLS" mild    && run_one "mild_classes_s${seed}" static "$seed" \
                            --cpu-mode quota --cpu-cores 1 --cpu-weight 20 --mem-frac 0.5 --squeeze 0
  has "$CELLS" squeeze && run_one "squeeze_classes_s${seed}" static "$seed"
done

[ "$DRY" = 1 ] && exit 0
if ! ls "$OUT"/*.jsonl >/dev/null 2>&1; then echo "!! no result files in $OUT"; exit 1; fi
python3 -m cf.linux.analyze "$OUT"/*.jsonl --csv "$OUT/summary.csv" >/dev/null || true
python3 -m cf.linux.classes "$OUT"/*.jsonl --out "$OUT" || true
[ "$FAILED" = 1 ] && echo "== some cells are incomplete; finish them with:  OUT=$OUT $0"
echo "== results in $OUT  (classes_report.md, classes_tables.xlsx, classes_interactive.csv, classes_compute.csv, classes_download.csv, plots/classes.png, plots/<run>_bench.png)"
