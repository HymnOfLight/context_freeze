#!/usr/bin/env bash
# Full sweep on the connected emulator: policies x eta x seeds, then a summary table + Pareto CSV/plot.
#
#   scripts/run_matrix.sh [CONFIG=configs/emulator_base.json] [T=40]
#   POLICIES="none lru landlord hybrid" ETAS="0.25 0.35 0.5" SEEDS="1 2 3" scripts/run_matrix.sh
#
# What the script guarantees (these are the things that made the first Pareto plot unreadable):
#   * one matrix = one output directory = one guest-RAM configuration; cf.analyze is run on THIS
#     directory only, never on results/matrix_*/* across different emulator configurations;
#   * every cell uses the same budget denominator: the first finished run's warmup m_fg is reused
#     (--m-fg-from) so eta means the same number of MB in every run;
#   * every (policy, eta) is replicated over SEEDS; the plot shows mean +/- sd and the `none`
#     baseline as a band, not as a curve over a parameter it does not use;
#   * the runner refuses to start (strict preflight) while Android's own freezer is active or the
#     emulator is software-rendered - run scripts/prepare_device.sh --system-freezer disabled and
#     scripts/start_emulator.sh first.
#
# Resume an interrupted sweep (finished cells are skipped, the unfinished one continues):
#   OUT=results/matrix_20260914-005939 scripts/run_matrix.sh configs/emulator_base.json 40
#   scripts/run_matrix.sh --resume results/matrix_20260914-005939 [CONFIG] [T]
#
# Android-default baseline (system freezer ON, policy none) is a separate experiment:
#   scripts/prepare_device.sh --system-freezer enabled
#   NO_STRICT=1 POLICIES=none OUT=results/android_default scripts/run_matrix.sh
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/env.sh

if [ "${1:-}" = "--resume" ]; then
  OUT="$2"; shift 2
fi
CONFIG="${1:-configs/emulator_base.json}"
T="${2:-40}"
POLICIES="${POLICIES:-none lru lfu landlord markov hybrid}"
ETAS="${ETAS:-0.25 0.35 0.5}"
SEEDS="${SEEDS:-1 2 3}"
OUT="${OUT:-results/matrix_$(date +%Y%m%d-%H%M%S)}"
M_FG_FROM="${M_FG_FROM:-}"
EXTRA=()
[ "${NO_STRICT:-0}" = 1 ] && EXTRA+=(--no-strict)
mkdir -p "$OUT"
export PYTHONUNBUFFERED=1

# host-side sanity: the emulator log tells us whether we fell back to software rendering
for elog in results/emulator_*.log; do
  [ -f "$elog" ] || continue
  if grep -qi "software gl\|swiftshader\|llvmpipe" "$elog"; then
    echo "!! $elog: emulator is using SOFTWARE rendering - latencies will be dominated by the GPU"
    echo "   fallback. Restart it with scripts/start_emulator.sh (forces -gpu host)."
  fi
done

# reuse the budget denominator of any run already finished in this directory (resume case)
if [ -z "$M_FG_FROM" ]; then
  for f in "$OUT"/*.jsonl; do
    [ -f "$f" ] && grep -q '"type": "budget"' "$f" && { M_FG_FROM="$f"; break; }
  done
fi

echo "== matrix dir: $OUT   (T=$T, policies: $POLICIES, etas: $ETAS, seeds: $SEEDS)"
[ -n "$M_FG_FROM" ] && echo "== budget denominator (m_fg) from: $M_FG_FROM"
echo "== interrupt any time; re-run with:  OUT=$OUT $0 $CONFIG $T"
FAILED=0

run_one() { # name policy eta seed
  local name="$1" pol="$2" eta="$3" seed="$4" rc
  local mfg=()
  [ -n "$M_FG_FROM" ] && mfg=(--m-fg-from "$M_FG_FROM")
  set +e
  python3 -u run_experiment.py "$CONFIG" --policy "$pol" --eta "$eta" --T "$T" --seed "$seed" \
    --name "$name" --out-dir "$OUT" "${mfg[@]}" "${EXTRA[@]}" 2>&1 | tee -a "$OUT/${name}.console.log"
  rc=${PIPESTATUS[0]}
  set -e
  if [ "$rc" = 130 ]; then                     # Ctrl+C: stop the whole sweep, keep checkpoints
    echo "== interrupted. Resume with:  OUT=$OUT $0 $CONFIG $T"; exit 130
  elif [ "$rc" = 2 ]; then                     # preflight refused: device must be fixed first
    echo "== preflight failed; fix the device (see messages above) and re-run:  OUT=$OUT $0 $CONFIG $T"; exit 2
  elif [ "$rc" != 0 ]; then
    echo "!! $name failed (exit $rc); continuing with the next cell"
    FAILED=1
  fi
  # first finished run fixes the denominator for all following cells
  if [ -z "$M_FG_FROM" ] && grep -q '"type": "budget"' "$OUT/${name}.jsonl" 2>/dev/null; then
    M_FG_FROM="$OUT/${name}.jsonl"
    echo "== budget denominator (m_fg) fixed from $M_FG_FROM for the rest of the matrix"
  fi
}

# baseline "none" ignores the budget: run it once per seed with eta=1.0
if echo " $POLICIES " | grep -q " none "; then
  for seed in $SEEDS; do run_one "none_eta1.0_s${seed}" none 1.0 "$seed"; done
  POLICIES=$(echo "$POLICIES" | sed 's/\bnone\b//')
fi

for pol in $POLICIES; do
  for eta in $ETAS; do
    for seed in $SEEDS; do
      run_one "${pol}_eta${eta}_s${seed}" "$pol" "$eta" "$seed"
    done
  done
done

python3 -m cf.analyze "$OUT"/*.jsonl --csv "$OUT/summary.csv" --agg "$OUT/summary_agg.csv" \
  --pareto "$OUT/pareto.csv" --plot "$OUT/pareto.png" || true
python3 -m cf.analyze "$OUT"/*.jsonl --x eta --plot "$OUT/pareto_eta.png" >/dev/null 2>&1 || true
if [ "$FAILED" = 1 ]; then
  echo "== some cells are incomplete; finish them with:  OUT=$OUT $0 $CONFIG $T"
fi
echo "== all results in $OUT  (pareto.png: saving vs latency/writes; pareto_eta.png: x = achieved eta)"
