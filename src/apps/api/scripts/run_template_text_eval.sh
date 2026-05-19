#!/usr/bin/env bash
# run_template_text_eval.sh — one-command live eval for nova.compose.template_text
#
# Runs the full live + judge eval suite for the TemplateTextAgent:
#   NOVA_EVAL_MODE=live pytest tests/evals/test_template_text_evals.py \
#     -v --eval-mode=live --with-judge --allow-cost
#
# Captures output to .dev/eval-results/template_text-<timestamp>.log and
# prints a one-line summary on exit. On failure, prints the last 20 lines
# of the log so you don't have to open the file.
#
# Cost: ~$2-5 per run (Gemini calls for each fixture + Claude Sonnet judge).
# When to run: after every bump of prompt_version in TemplateTextAgent's AgentSpec,
# and to record a v0.4.26.0 baseline before editing any template_text prompt.
#
# See: src/apps/api/tests/evals/README.md for the full prompt-iteration loop.
#      CLAUDE.md "Agent evals" section for the prompt-change rule.
#
# Usage: bash scripts/run_template_text_eval.sh [--help|-h]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
API_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$API_DIR/../../.." && pwd)"

# ── Help ─────────────────────────────────────────────────────────────────────

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'EOF'
run_template_text_eval.sh — live eval wrapper for nova.compose.template_text

USAGE
  cd <repo-root>
  bash src/apps/api/scripts/run_template_text_eval.sh

  or from src/apps/api/:
  bash scripts/run_template_text_eval.sh

WHAT IT DOES
  Runs the template_text eval suite in live mode with the LLM-as-judge:
    NOVA_EVAL_MODE=live pytest tests/evals/test_template_text_evals.py \
      -v --eval-mode=live --with-judge --allow-cost

  Output is tee'd to .dev/eval-results/template_text-<YYYYMMDD-HHMMSS>.log.
  On success: prints a one-line summary.
  On failure: prints the summary + last 20 lines of the log.

COST
  ~$2-5 per run (Gemini API calls per fixture + Claude Sonnet judge).
  The harness pre-flights an estimated cost at collection time and aborts
  if it exceeds the $20 cap (--allow-cost is pre-set by this wrapper).

WHEN TO RUN
  1. To record the v0.4.26.0 baseline before touching any template_text prompt.
  2. After every bump of prompt_version in TemplateTextAgent's AgentSpec
     (per CLAUDE.md "Prompt-change rule").

PREREQUISITES
  Export (ANTHROPIC_API_KEY) and (GEMINI_API_KEY) in your environment,
  or set them in your repo-root .env and export before calling this script.

  Fixtures must exist under:
    tests/fixtures/agent_evals/template_text/prod_snapshots/
  Build them with:
    cd src/apps/api
    .venv/bin/python scripts/export_eval_fixtures.py --only template_text

  Ground-truth overlays (optional, recommended for hard-floor scoring):
    cd src/apps/api
    .venv/bin/python scripts/build_text_ground_truth.py

SEE ALSO
  src/apps/api/tests/evals/README.md — full prompt-iteration loop
  CLAUDE.md — "Agent evals" section
EOF
  exit 0
fi

# ── Guard: unexpected arguments ───────────────────────────────────────────────

if [[ $# -gt 0 ]]; then
  echo "error: unknown argument '$1'" >&2
  echo "Run with --help for usage." >&2
  exit 1
fi

# ── Guard: required env vars ─────────────────────────────────────────────────

missing_keys=()

if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
  missing_keys+=("ANTHROPIC_API_KEY")
fi

if [[ -z "${GEMINI_API_KEY:-}" ]]; then
  missing_keys+=("GEMINI_API_KEY")
fi

if [[ ${#missing_keys[@]} -gt 0 ]]; then
  echo "error: missing required API key(s):" >&2
  for key in "${missing_keys[@]}"; do
    echo "  $key is not set or empty" >&2
  done
  echo "" >&2
  echo "Set them in your shell before calling this script:" >&2
  echo "  export ANTHROPIC_API_KEY=sk-ant-..." >&2
  echo "  export GEMINI_API_KEY=AIza..." >&2
  echo "" >&2
  echo "Or add them to the repo-root .env file and source it:" >&2
  echo "  source $REPO_ROOT/.env" >&2
  exit 2
fi

# ── Guard: prod_snapshots must not be empty ───────────────────────────────────

SNAPSHOTS_DIR="$API_DIR/tests/fixtures/agent_evals/template_text/prod_snapshots"

if [[ ! -d "$SNAPSHOTS_DIR" ]] || [[ -z "$(ls -A "$SNAPSHOTS_DIR" 2>/dev/null)" ]]; then
  echo "error: no prod_snapshots yet — see scripts/export_eval_fixtures.py --only template_text" >&2
  echo "" >&2
  echo "Run from src/apps/api/:" >&2
  echo "  .venv/bin/python scripts/export_eval_fixtures.py --only template_text" >&2
  exit 2
fi

# ── Prepare output dir + log file ─────────────────────────────────────────────

RESULTS_DIR="$REPO_ROOT/.dev/eval-results"
mkdir -p "$RESULTS_DIR"

TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
LOG_FILE="$RESULTS_DIR/template_text-$TIMESTAMP.log"

echo "[run_template_text_eval] starting live eval — log: $LOG_FILE"

# ── Run pytest ────────────────────────────────────────────────────────────────

EXIT_CODE=0
(
  cd "$API_DIR"
  NOVA_EVAL_MODE=live \
    .venv/bin/pytest tests/evals/test_template_text_evals.py \
      -v --eval-mode=live --with-judge --allow-cost
) 2>&1 | tee "$LOG_FILE" || EXIT_CODE=$?

# ── One-line summary ──────────────────────────────────────────────────────────

# Grep for pytest's final summary line, e.g.:
#   "3 passed in 4m12.34s"  /  "2 passed, 1 failed in 3m01s"  / "1 error"
SUMMARY_LINE="$(grep -E '^(FAILED|ERROR|=+ .+ =+$)' "$LOG_FILE" | tail -1 || true)"
PYTEST_RESULT="$(grep -E '[0-9]+ (passed|failed|error)' "$LOG_FILE" | tail -1 | sed 's/^[= ]*//' | sed 's/[= ]*$//' || true)"

if [[ -z "$PYTEST_RESULT" ]]; then
  PYTEST_RESULT="(no summary line found — check log)"
fi

REL_LOG="${LOG_FILE#"$REPO_ROOT/"}"

echo ""
echo "template_text live eval: $PYTEST_RESULT — log: $REL_LOG"

# On failure: show last 20 lines so the error is visible without opening the file.
if [[ $EXIT_CODE -ne 0 ]]; then
  echo ""
  echo "--- last 20 lines of log ---"
  tail -20 "$LOG_FILE"
  echo "----------------------------"
fi

exit $EXIT_CODE
