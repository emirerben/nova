#!/usr/bin/env bash
# Run selected Main Creator eval fixtures live, split into parallel batches, each its own
# development-ledger run capped at $2. Writes one JUnit file per batch into OUT_DIR.
#
# usage: run_live_batches.sh RUN_PREFIX BATCHES MODEL EFFORT DB_NAME OUT_DIR NODE_FILE
#   NODE_FILE: one pytest node id per line (relative to src/apps/api)
# then: summarize_live_eval.py OUT.json DB_NAME RUN_PREFIX OUT_DIR/RUN_PREFIX-b*.xml
set -euo pipefail

prefix=$1 batches=$2 model=$3 effort=$4 db=$5 out=$6 nodes=$7
root=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
api="$root/src/apps/api"
mkdir -p "$out"

set -a
# shellcheck disable=SC1091
source "$root/.env"
set +a
export DATABASE_URL="postgresql://$(whoami)@localhost:5432/$db"
export NOVA_EVAL_MODE=live AI_COST_CONTROL_ENABLED=true AI_USAGE_ENVIRONMENT=development
export MAIN_CREATOR_MODEL="$model" MAIN_CREATOR_THINKING_LEVEL="$effort"
export AI_OPENAI_MONTHLY_BUDGET_USD=25 AI_OPENAI_PLANNING_BUDGET_USD=25
# A live_eval call is refused once the environment spend passes 80% of this budget.
export AI_DEVELOPMENT_MONTHLY_BUDGET_USD="${DEV_BUDGET_USD:-$AI_DEVELOPMENT_MONTHLY_BUDGET_USD}"
# The model's raw answer for every fixture whose answer parsed, failing assertions included.
export NOVA_EVAL_CAPTURE_DIR="$out/$prefix-capture"

pids=()
for ((i = 0; i < batches; i++)); do
  run="$prefix-b$((i + 1))"
  selected=()
  while IFS= read -r node; do selected+=("$node"); done < <(
    awk -v n="$batches" -v i="$i" 'NF && (NR - 1) % n == i' "$nodes"
  )
  (
    cd "$api"
    .venv/bin/python -m pytest "${selected[@]}" -p no:cacheprovider -q --eval-mode=live \
      --usage-purpose=live_eval --test-run-id="$run" --max-cost-usd=2 --approve-reservation \
      --junitxml="$out/$run.xml" >"$out/$run.log" 2>&1 || true
    echo "$run: $(tail -1 "$out/$run.log")"
  ) &
  pids+=($!)
done
wait "${pids[@]}"
