#!/usr/bin/env bash
# Record one KRI-797 arm (tools mode) over the request-following set in $2-capped batches.
# Each batch is its own development-ledger run (RUN_PREFIX-a, -b, ...); a batch stops before the
# turn that could cross $2 and the next batch resumes there.
#
# usage: run_tools_arm.sh ARM OUT_DIR RUN_PREFIX [TAG] [ONLY]
#   ARM: gemini | sol | sonnet     TAG: a rerun's label     ONLY: comma-separated fixture ids
set -euo pipefail

arm=$1 out=$2 prefix=$3 tag=${4:-} only=${5:-}
root=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
cd "$root/src/apps/api"
set -a
# shellcheck disable=SC1091
source "$root/.env"
set +a
export DATABASE_URL="$(cat "$root/.dev/test-db-url")"
export NOVA_EVAL_MODE=live AI_COST_CONTROL_ENABLED=true AI_USAGE_ENVIRONMENT=development
export AI_OPENAI_MONTHLY_BUDGET_USD=20 AI_OPENAI_PLANNING_BUDGET_USD=20
export AI_ANTHROPIC_MONTHLY_BUDGET_USD=20 AI_ANTHROPIC_PLANNING_BUDGET_USD=20

for batch in a b c d e f g h i j k l; do
  log="$out/$prefix-$batch.log"
  .venv/bin/python -m tests.evals.request_following.compare_tools --arm "$arm" --out "$out" \
    ${tag:+--tag "$tag"} ${only:+--only "$only"} \
    --usage-purpose live_eval --test-run-id "$prefix-$batch" --max-cost-usd 2 \
    --approve-reservation >"$log" 2>&1 || { echo "$prefix-$batch failed"; tail -5 "$log"; exit 1; }
  grep -E "^run " "$log"
  grep -q "^cap:" "$log" || break
done
