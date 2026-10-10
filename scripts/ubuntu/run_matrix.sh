#!/usr/bin/env bash
# Sweep on the Ubuntu desktop: scenarios x policies x eta x seeds (+ optional throttle-profile variants),
# one output directory, same budget denominator for every cell, summary + Pareto plot at the end.
#
#   scripts/ubuntu/run_matrix.sh [CONFIG=configs/ubuntu_cn_desktop.json] [T=60]
#   SCENARIOS="office download chat design" POLICIES="none static landlord hybrid" ETAS="0.25 0.5" SEEDS="1" \
#       OUT=results/ubuntu_matrix scripts/ubuntu/run_matrix.sh configs/ubuntu_cn_desktop.json 60
#   PROFILES="1c:0.5 1c:0.3 2c:0.5" scripts/ubuntu/run_matrix.sh      # cpu cores : mem_frac variants (static policy)
#   QUICK=1 scripts/ubuntu/run_matrix.sh                                # smoke test: office, none + static, T=15
#
# `none` and `static` ignore eta and are run once per scenario / seed (eta=1.0 and 0.0 in the file name).
# Interrupt any time; re-run the same command with the same OUT to continue (finished cells are skipped).
set -euo pipefail
cd "$(dirname "$0")/../.."

if [ "${1:-}" = "--resume" ]; then OUT="$2"; shift 2; fi
CONFIG="${1:-configs/ubuntu_cn_desktop.json}"
T="${2:-60}"
if [ "${QUICK:-0}" = 1 ]; then
  SCENARIOS="${SCENARIOS:-office}"; POLICIES="${POLICIES:-none static}"; ETAS="${ETAS:-0.5}"; SEEDS="${SEEDS:-1}"; T=15
fi
POLICIES="${POLICIES:-none static lru landlord hybrid}"
ETAS="${ETAS:-0.25 0.5}"
SEEDS="${SEEDS:-1}"
SCENARIOS="${SCENARIOS:-office download chat design}"
PROFILES="${PROFILES:-}"            # e.g. "1c:0.5 1c:0.3": cpu_cores:mem_frac variants for the static policy
OUT="${OUT:-results/ubuntu_matrix_$(date +%Y%m%d-%H%M%S)}"
M_FG_FROM="${M_FG_FROM:-}"
EXTRA="${EXTRA:-}"                  # extra run_linux.py flags, e.g. "--no-resident-cpu"
mkdir -p "$OUT"
export PYTHONUNBUFFERED=1

if ! grep -q '^0::/cf/' /proc/self/cgroup; then
  echo "!! this shell is not under /cf; run  source scripts/ubuntu/cf_enter.sh  first" >&2; exit 2
fi
if [ -z "$M_FG_FROM" ]; then
  for f in "$OUT"/*.jsonl; do [ -f "$f" ] && grep -q '"type": "budget"' "$f" && { M_FG_FROM="$f"; break; }; done
fi
echo "== matrix dir: $OUT  (T=$T, scenarios: $SCENARIOS, policies: $POLICIES, etas: $ETAS, seeds: $SEEDS${PROFILES:+, profiles: $PROFILES})"
[ -n "$M_FG_FROM" ] && echo "== budget denominator (m_fg) from: $M_FG_FROM"

python3 - "$CONFIG" "$T" "$OUT" "$POLICIES" "$ETAS" "$SEEDS" "$SCENARIOS" "$PROFILES" <<'PYEOF' || true
import glob, json, os, sys
cfg_path, T, out, pols, etas, seeds, scs, profs = sys.argv[1:9]
cfg = json.load(open(cfg_path)); T = int(T)
pols = pols.split(); n_eta = len(etas.split()); n_seed = len(seeds.split()); n_sc = max(1, len(scs.split()))
flat = [p for p in pols if p in ("none", "static")]; budgeted = [p for p in pols if p not in ("none", "static")]
cells = n_sc * n_seed * (len(flat) + len(budgeted) * n_eta + len(profs.split()))
done = sum(1 for f in glob.glob(os.path.join(out, "*.jsonl")) if '"type": "end"' in open(f, "rb").read()[-4096:].decode("utf-8", "replace"))
n_apps = len(cfg.get("apps", []))
step_s = cfg.get("dwell_s", 8.0) + cfg.get("settle_s", 2.0) + 3.0
cell_s = T * step_s + n_apps * (cfg.get("warmup_dwell_s", 10.0) + 3) + 30
print(f"== {cells} cells ({done} finished), ~{cell_s / 60:.0f} min each -> ~{max(0, cells - done) * cell_s / 3600:.1f} h remaining")
PYEOF
echo "== interrupt any time; re-run with:  OUT=$OUT $0 $CONFIG $T"
FAILED=0

run_one() { # name policy eta seed scenario [extra flags...]
  local name="$1" pol="$2" eta="$3" seed="$4" sc="$5" rc; shift 5
  local cmd=(python3 -u run_linux.py "$CONFIG" --policy "$pol" --eta "$eta" --T "$T" --seed "$seed"
             --name "$name" --out-dir "$OUT" --scenario "$sc")
  [ -n "$M_FG_FROM" ] && cmd+=(--m-fg-from "$M_FG_FROM")
  [ "${NO_STRICT:-0}" = 1 ] && cmd+=(--no-strict)
  # shellcheck disable=SC2206
  [ -n "$EXTRA" ] && cmd+=($EXTRA)
  [ $# -gt 0 ] && cmd+=("$@")
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

for sc in $SCENARIOS; do
  for seed in $SEEDS; do
    echo " $POLICIES " | grep -q " none " && run_one "none_${sc}_eta1.0_s${seed}" none 1.0 "$seed" "$sc"
    echo " $POLICIES " | grep -q " static " && run_one "static_${sc}_eta0.0_s${seed}" static 0.0 "$seed" "$sc"
    for prof in $PROFILES; do        # "1c:0.5" -> --cpu-cores 1 --mem-frac 0.5 with the static policy
      cores="${prof%%c:*}"; frac="${prof#*:}"
      run_one "static_${sc}_${cores}c_m${frac}_s${seed}" static 0.0 "$seed" "$sc" --cpu-cores "$cores" --mem-frac "$frac"
    done
  done
done
for pol in $POLICIES; do
  [ "$pol" = none ] || [ "$pol" = static ] && continue
  for eta in $ETAS; do for sc in $SCENARIOS; do for seed in $SEEDS; do
    run_one "${pol}_${sc}_eta${eta}_s${seed}" "$pol" "$eta" "$seed" "$sc"
  done; done; done
done

if ! ls "$OUT"/*.jsonl >/dev/null 2>&1; then echo "!! no result files in $OUT"; exit 1; fi
python3 -m cf.linux.analyze "$OUT"/*.jsonl --csv "$OUT/summary.csv" --agg "$OUT/summary_agg.csv" \
  --pareto "$OUT/pareto.csv" --plot "$OUT/pareto.png" || true
[ "$FAILED" = 1 ] && echo "== some cells are incomplete; finish them with:  OUT=$OUT $0 $CONFIG $T"
echo "== all results in $OUT"
