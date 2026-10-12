#!/usr/bin/env bash
# Sweep on the Ubuntu desktop: scenarios x policies x eta x seeds, plus throttle-strength variants,
# one output directory, same budget denominator for every cell, summary / per-app table / plots /
# Markdown report at the end.
#
#   scripts/ubuntu/run_matrix.sh [CONFIG=configs/ubuntu_cn_desktop.json] [T=60]
#   SCENARIOS="office download chat design day" POLICIES="none static lru landlord hybrid" ETAS="0.6 0.8" SEEDS="1 2" \
#       OUT=results/ubuntu_matrix scripts/ubuntu/run_matrix.sh configs/ubuntu_cn_desktop.json 60
#   PROFILES="1c:0.5 1c:0.3 0.5c:0.5 2c:0.5" scripts/ubuntu/run_matrix.sh    # cpu cores : mem_frac variants (static policy)
#   CPU_MODES="quota cpuset" scripts/ubuntu/run_matrix.sh                     # quota (1 core each) vs cpuset (all bg apps share 1 core)
#   RESIDENT="1 0" scripts/ubuntu/run_matrix.sh                               # residents CPU-limited too (1) or untouched (0), budgeted policies
#   SWAP_CAPS="512 128" scripts/ubuntu/run_matrix.sh                          # memory.swap.max per shrunk app (static policy)
#   MEM_HARD=1 scripts/ubuntu/run_matrix.sh                                   # contrast cell: memory.max on (expect oom / state_lost > 0)
#   MEM_FRAC=0.3 ETAS="0.4 0.6" scripts/ubuntu/run_matrix.sh                  # whole matrix with a smaller memory.high (lower feasible eta)
#   NIGHT=1 scripts/ubuntu/run_matrix.sh configs/ubuntu_cn_desktop.json 40    # one night (~50 cells, ~10 h): 4 scenarios, 5 policies, 2 etas + variants
#   FULL=1 scripts/ubuntu/run_matrix.sh configs/ubuntu_cn_desktop.json 60     # everything above with full lists (~350 cells, several days; resumable)
#   QUICK=1 scripts/ubuntu/run_matrix.sh                                      # smoke test: office, none + static, T=15
#   PLACEHOLDERS=1 scripts/ubuntu/run_matrix.sh                               # apps not installed yet run as placeholders (same ids)
#   DRY=1 FULL=1 scripts/ubuntu/run_matrix.sh                                 # only list the cells / commands, run nothing
#
# `none` and `static` ignore eta and are run once per scenario / seed (eta=1.0 and 0.0 in the file name).
# eta must stay above the shrunk floor: a shrunk app keeps memory.high = mem_frac x m_fg (>= mem_min_mb),
# so with mem_frac 0.5 the background can never go under ~0.5 x sum(m_fg) and eta < ~0.5 degenerates
# to static (the runner warns, analyze reports eta_min / eta_feasible).  Hence the defaults 0.6 / 0.8.
# Interrupt any time; re-run the same command with the same OUT to continue (finished cells are skipped).
set -euo pipefail
cd "$(dirname "$0")/../.."

if [ "${1:-}" = "--resume" ]; then OUT="$2"; shift 2; fi
CONFIG="${1:-configs/ubuntu_cn_desktop.json}"
T="${2:-60}"
if [ "${QUICK:-0}" = 1 ]; then
  SCENARIOS="${SCENARIOS:-office}"; POLICIES="${POLICIES:-none static}"; ETAS="${ETAS:-0.7}"; SEEDS="${SEEDS:-1}"; T=15
fi
if [ "${NIGHT:-0}" = 1 ]; then
  SCENARIOS="${SCENARIOS:-office download chat design}"
  POLICIES="${POLICIES:-none static lru landlord hybrid}"
  ETAS="${ETAS:-0.6 0.8}"
  SEEDS="${SEEDS:-1}"
  PROFILES="${PROFILES:-1c:0.3 0.5c:0.5}"
  CPU_MODES="${CPU_MODES:-cpuset}"
  SWAP_CAPS="${SWAP_CAPS:-128}"
  MEM_HARD="${MEM_HARD:-1}"
fi
if [ "${FULL:-0}" = 1 ]; then
  SCENARIOS="${SCENARIOS:-office download chat design day}"
  POLICIES="${POLICIES:-none static lru landlord hybrid markov}"
  ETAS="${ETAS:-0.55 0.7 0.85}"
  SEEDS="${SEEDS:-1 2}"
  PROFILES="${PROFILES:-1c:0.5 1c:0.3 1c:0.7 0.5c:0.5 2c:0.5}"
  CPU_MODES="${CPU_MODES:-quota cpuset}"
  RESIDENT="${RESIDENT:-1 0}"
  SWAP_CAPS="${SWAP_CAPS:-512 128}"
  MEM_HARD="${MEM_HARD:-1}"
fi
POLICIES="${POLICIES:-none static lru landlord hybrid}"
ETAS="${ETAS:-0.6 0.8}"
SEEDS="${SEEDS:-1}"
SCENARIOS="${SCENARIOS:-office download chat design}"
PROFILES="${PROFILES:-}"            # "1c:0.5 1c:0.3": cpu_cores:mem_frac variants (static policy)
CPU_MODES="${CPU_MODES:-}"          # "quota cpuset": cpu_mode variants (static policy; quota is the default run)
RESIDENT="${RESIDENT:-1}"           # "1 0": cpu_throttle_resident on / off for the budgeted policies
SWAP_CAPS="${SWAP_CAPS:-}"          # "512 128": --swap-max-mb variants (static policy)
MEM_HARD="${MEM_HARD:-0}"           # 1: one static cell per scenario with --mem-hard (OOM contrast)
MEM_FRAC="${MEM_FRAC:-}"            # e.g. 0.3: --mem-frac for every cell (file names get _mf0.3)
OUT="${OUT:-results/ubuntu_matrix_$(date +%Y%m%d-%H%M%S)}"
M_FG_FROM="${M_FG_FROM:-}"
EXTRA="${EXTRA:-}"                  # extra run_linux.py flags, e.g. "--sample-every 2 --dwell 5 12"
[ "${PLACEHOLDERS:-0}" = 1 ] && EXTRA="$EXTRA --placeholders"
[ -n "$MEM_FRAC" ] && EXTRA="$EXTRA --mem-frac $MEM_FRAC"
SUFFIX="${MEM_FRAC:+_mf$MEM_FRAC}"
DRY="${DRY:-0}"
[ "$DRY" = 1 ] || mkdir -p "$OUT"
export PYTHONUNBUFFERED=1

if [ "$DRY" != 1 ] && ! grep -q '^0::/cf/' /proc/self/cgroup; then
  echo "!! this shell is not under /cf; run  source scripts/ubuntu/cf_enter.sh  first" >&2; exit 2
fi
if [ -z "$M_FG_FROM" ]; then
  for f in "$OUT"/*.jsonl; do [ -f "$f" ] && grep -q '"type": "budget"' "$f" && { M_FG_FROM="$f"; break; }; done
fi
echo "== matrix dir: $OUT  (T=$T, scenarios: $SCENARIOS, policies: $POLICIES, etas: $ETAS, seeds: $SEEDS" \
     "${PROFILES:+, profiles: $PROFILES}${CPU_MODES:+, cpu modes: $CPU_MODES}, resident: $RESIDENT${SWAP_CAPS:+, swap caps: $SWAP_CAPS}" \
     "${MEM_HARD:+, mem_hard contrast: $MEM_HARD}${EXTRA:+, extra: $EXTRA})"
[ -n "$M_FG_FROM" ] && echo "== budget denominator (m_fg) from: $M_FG_FROM"
[ "$DRY" = 1 ] || { echo "date=$(date -Is)"; echo "config=$CONFIG"; echo "T=$T"; echo "scenarios=$SCENARIOS"; echo "policies=$POLICIES";
  echo "etas=$ETAS"; echo "seeds=$SEEDS"; echo "profiles=$PROFILES"; echo "cpu_modes=$CPU_MODES"; echo "resident=$RESIDENT";
  echo "swap_caps=$SWAP_CAPS"; echo "mem_hard=$MEM_HARD"; echo "mem_frac=$MEM_FRAC"; echo "extra=$EXTRA"; echo "kernel=$(uname -r)";
  echo "ram_mb=$(( $(grep MemTotal /proc/meminfo | awk '{print $2}') / 1024 ))"; } > "$OUT/matrix.env"

python3 - "$CONFIG" "$T" "$OUT" "$POLICIES" "$ETAS" "$SEEDS" "$SCENARIOS" "$PROFILES" "$CPU_MODES" "$RESIDENT" "$SWAP_CAPS" "$MEM_HARD" <<'PYEOF' || true
import glob, json, os, sys
cfg_path, T, out, pols, etas, seeds, scs, profs, modes, res, swaps, hard = sys.argv[1:13]
cfg = json.load(open(cfg_path)); T = int(T)
pols = pols.split(); n_eta = len(etas.split()); n_seed = len(seeds.split()); n_sc = max(1, len(scs.split()))
flat = [p for p in pols if p in ("none", "static")]; budgeted = [p for p in pols if p not in ("none", "static")]
n_modes = max(0, len(modes.split()) - (1 if "quota" in modes.split() else 0))
per_sc_seed = len(flat) + len(profs.split()) + n_modes + len(swaps.split()) + (1 if hard == "1" else 0) \
    + len(budgeted) * n_eta * len(res.split())
cells = n_sc * n_seed * per_sc_seed
done = sum(1 for f in glob.glob(os.path.join(out, "*.jsonl")) if '"type": "end"' in open(f, "rb").read()[-4096:].decode("utf-8", "replace"))
n_apps = len(cfg.get("apps", []))
d = cfg.get("dwell_s", 8.0); d = sum(d) / 2 if isinstance(d, list) else d
step_s = d + cfg.get("settle_s", 2.0) + 3.0
cell_s = T * step_s + n_apps * (cfg.get("warmup_dwell_s", 10.0) + 3) + 30
print(f"== {cells} cells ({done} finished), ~{cell_s / 60:.0f} min each -> ~{max(0, cells - done) * cell_s / 3600:.1f} h remaining")
PYEOF
echo "== interrupt any time; re-run with:  OUT=$OUT $0 $CONFIG $T"
FAILED=0

run_one() { # name policy eta seed scenario [extra flags...]
  local name="$1$SUFFIX" pol="$2" eta="$3" seed="$4" sc="$5" rc; shift 5
  if [ -f "$OUT/${name}.jsonl" ] && tail -c 4096 "$OUT/${name}.jsonl" | grep -q '"type": "end"'; then
    echo "== $name already finished - skipping"; return 0
  fi
  local cmd=(python3 -u run_linux.py "$CONFIG" --policy "$pol" --eta "$eta" --T "$T" --seed "$seed"
             --name "$name" --out-dir "$OUT" --scenario "$sc")
  [ -n "$M_FG_FROM" ] && cmd+=(--m-fg-from "$M_FG_FROM")
  [ "${NO_STRICT:-0}" = 1 ] && cmd+=(--no-strict)
  # shellcheck disable=SC2206
  [ -n "$EXTRA" ] && cmd+=($EXTRA)
  [ $# -gt 0 ] && cmd+=("$@")
  [ -f "$OUT/${name}.ckpt" ] && cmd+=(--resume)
  if [ "$DRY" = 1 ]; then echo "${cmd[*]}"; return 0; fi
  set +e
  "${cmd[@]}" 2>&1 | tee -a "$OUT/${name}.console.log"
  rc=${PIPESTATUS[0]}
  set -e
  if [ "$rc" = 130 ]; then echo "== interrupted. Resume with:  OUT=$OUT $0 $CONFIG $T"; exit 130
  elif [ "$rc" = 2 ]; then echo "== preflight failed; fix the environment and re-run:  OUT=$OUT $0 $CONFIG $T"; exit 2
  elif [ "$rc" != 0 ]; then echo "!! $name failed (exit $rc); continuing"; FAILED=1; fi
  if [ -z "$M_FG_FROM" ] && grep -q '"type": "budget"' "$OUT/${name}.jsonl" 2>/dev/null; then
    M_FG_FROM="$OUT/${name}.jsonl"; echo "== budget denominator (m_fg) fixed from $M_FG_FROM"
  fi
}

has() { echo " $1 " | grep -q " $2 "; }

# 1. baselines and throttle-strength variants (static: everything but the foreground is shrunk)
for sc in $SCENARIOS; do
  for seed in $SEEDS; do
    has "$POLICIES" none   && run_one "none_${sc}_eta1.0_s${seed}" none 1.0 "$seed" "$sc"
    has "$POLICIES" static && run_one "static_${sc}_eta0.0_s${seed}" static 0.0 "$seed" "$sc"
    for prof in $PROFILES; do        # "1c:0.5" -> --cpu-cores 1 --mem-frac 0.5
      cores="${prof%%c:*}"; frac="${prof#*:}"
      run_one "static_${sc}_${cores}c_m${frac}_s${seed}" static 0.0 "$seed" "$sc" --cpu-cores "$cores" --mem-frac "$frac"
    done
    for mode in $CPU_MODES; do       # quota is the default run above; cpuset / both / none are variants
      [ "$mode" = quota ] && continue
      run_one "static_${sc}_${mode}_s${seed}" static 0.0 "$seed" "$sc" --cpu-mode "$mode"
    done
    for cap in $SWAP_CAPS; do
      run_one "static_${sc}_swap${cap}_s${seed}" static 0.0 "$seed" "$sc" --swap-max-mb "$cap"
    done
    [ "$MEM_HARD" = 1 ] && run_one "static_${sc}_memhard_s${seed}" static 0.0 "$seed" "$sc" --mem-hard
  done
done
# 2. budgeted policies: eta sweep, residents CPU-limited (default) and untouched
for pol in $POLICIES; do
  [ "$pol" = none ] || [ "$pol" = static ] && continue
  for r in $RESIDENT; do
    for eta in $ETAS; do for sc in $SCENARIOS; do for seed in $SEEDS; do
      if [ "$r" = 1 ]; then run_one "${pol}_${sc}_eta${eta}_s${seed}" "$pol" "$eta" "$seed" "$sc"
      else run_one "${pol}_${sc}_eta${eta}_nores_s${seed}" "$pol" "$eta" "$seed" "$sc" --no-resident-cpu; fi
    done; done; done
  done
done

[ "$DRY" = 1 ] && exit 0
if ! ls "$OUT"/*.jsonl >/dev/null 2>&1; then echo "!! no result files in $OUT"; exit 1; fi
python3 -m cf.linux.analyze "$OUT"/*.jsonl --csv "$OUT/summary.csv" --agg "$OUT/summary_agg.csv" \
  --per-app "$OUT/per_app.csv" --pareto "$OUT/pareto.csv" --plot "$OUT/plots/pareto.png" \
  --plots-dir "$OUT/plots" --md "$OUT/report.md" || true
[ "$FAILED" = 1 ] && echo "== some cells are incomplete; finish them with:  OUT=$OUT $0 $CONFIG $T"
echo "== all results in $OUT  (report.md, summary.csv, summary_agg.csv, per_app.csv, plots/)"
