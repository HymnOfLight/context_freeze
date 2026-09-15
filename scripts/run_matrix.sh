#!/usr/bin/env bash
# Full sweep on the connected emulator: policies x eta, then a summary table + Pareto CSV/plot.
#
#   scripts/run_matrix.sh [CONFIG=configs/emulator_base.json] [T=40]
#   POLICIES="none lru landlord hybrid" ETAS="0.2 0.3 0.5" scripts/run_matrix.sh
#
# Resume an interrupted sweep (finished (policy, eta) pairs are skipped, the unfinished one
# continues from its checkpoint, the remaining ones run as usual):
#
#   OUT=results/matrix_20260914-005939 scripts/run_matrix.sh configs/emulator_base.json 40
#   scripts/run_matrix.sh --resume results/matrix_20260914-005939 [CONFIG] [T]
#
# All output (console + JSONL + checkpoints) lives in $OUT; nothing is moved around afterwards.
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/env.sh

if [ "${1:-}" = "--resume" ]; then
  OUT="$2"; shift 2
fi
CONFIG="${1:-configs/emulator_base.json}"
T="${2:-40}"
POLICIES="${POLICIES:-none lru lfu landlord markov hybrid}"
ETAS="${ETAS:-0.2 0.3 0.5}"
OUT="${OUT:-results/matrix_$(date +%Y%m%d-%H%M%S)}"
mkdir -p "$OUT"
export PYTHONUNBUFFERED=1

# host-side sanity: the emulator log tells us whether we fell back to software rendering
for elog in results/emulator_*.log; do
  [ -f "$elog" ] || continue
  if grep -qi "software gl\|swiftshader\|llvmpipe" "$elog"; then
    echo "!! $elog: emulator is using SOFTWARE rendering - latencies will be dominated by the GPU"
    echo "   fallback. Free host memory (close IDE/browser) and restart the emulator (-gpu auto)."
  fi
done

echo "== matrix dir: $OUT   (T=$T, policies: $POLICIES, etas: $ETAS)"
echo "== interrupt any time; re-run with:  OUT=$OUT $0 $CONFIG $T"
FAILED=0

run_one() { # name policy eta
  local name="$1" pol="$2" eta="$3" rc
  set +e
  python3 -u run_experiment.py "$CONFIG" --policy "$pol" --eta "$eta" --T "$T" \
    --name "$name" --out-dir "$OUT" 2>&1 | tee -a "$OUT/${name}.console.log"
  rc=${PIPESTATUS[0]}
  set -e
  if [ "$rc" = 130 ]; then                     # Ctrl+C: stop the whole sweep, keep checkpoints
    echo "== interrupted. Resume with:  OUT=$OUT $0 $CONFIG $T"; exit 130
  elif [ "$rc" != 0 ]; then
    echo "!! $name failed (exit $rc); continuing with the next cell"
    FAILED=1
  fi
}

# baseline "none" ignores the budget: run it once with eta=1.0
if echo " $POLICIES " | grep -q " none "; then
  run_one none_eta1.0 none 1.0
  POLICIES=$(echo "$POLICIES" | sed 's/\bnone\b//')
fi

for pol in $POLICIES; do
  for eta in $ETAS; do
    run_one "${pol}_eta${eta}" "$pol" "$eta"
  done
done

python3 -m cf.analyze "$OUT"/*.jsonl --csv "$OUT/summary.csv" --pareto "$OUT/pareto.csv" --plot "$OUT/pareto.png" || true
if [ "$FAILED" = 1 ]; then
  echo "== some cells are incomplete; finish them with:  OUT=$OUT $0 $CONFIG $T"
fi
echo "== all results in $OUT"
