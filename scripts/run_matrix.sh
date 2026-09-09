#!/usr/bin/env bash
# Full sweep on the connected emulator: policies x eta, then a summary table + Pareto CSV/plot.
#
#   scripts/run_matrix.sh [CONFIG=configs/emulator_base.json] [T=40]
#   POLICIES="none lru landlord hybrid" ETAS="0.2 0.3 0.5" scripts/run_matrix.sh
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/env.sh

CONFIG="${1:-configs/emulator_base.json}"
T="${2:-40}"
POLICIES="${POLICIES:-none lru lfu landlord markov hybrid}"
ETAS="${ETAS:-0.2 0.3 0.5}"
STAMP=$(date +%Y%m%d-%H%M%S)
OUT="results/matrix_${STAMP}"
mkdir -p "$OUT"

# baseline "none" ignores the budget: run it once with eta=1.0
if echo " $POLICIES " | grep -q " none "; then
  python3 run_experiment.py "$CONFIG" --policy none --eta 1.0 --T "$T" --name "none_eta1.0" \
    | tee "$OUT/none.log"
  mv results/none_eta1.0.jsonl "$OUT/"
  POLICIES=$(echo "$POLICIES" | sed 's/\bnone\b//')
fi

for pol in $POLICIES; do
  for eta in $ETAS; do
    python3 run_experiment.py "$CONFIG" --policy "$pol" --eta "$eta" --T "$T" --name "${pol}_eta${eta}" \
      | tee "$OUT/${pol}_eta${eta}.log"
    mv "results/${pol}_eta${eta}.jsonl" "$OUT/"
  done
done

python3 -m cf.analyze "$OUT"/*.jsonl --csv "$OUT/summary.csv" --pareto "$OUT/pareto.csv" --plot "$OUT/pareto.png" || true
echo "== all results in $OUT"
