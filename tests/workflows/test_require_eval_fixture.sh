#!/usr/bin/env bash
#
# Tests for the T8 require-eval-fixture gate. Same shape as the T6 tests —
# we run the same scripts the workflow runs, in the same composition order.

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
# shellcheck source=./_assert.sh
. "$HERE/_assert.sh"

PATHS_SCRIPT="$REPO/scripts/ci/check-touched-paths.sh"
BODY_SCRIPT="$REPO/scripts/ci/check-pr-body.sh"

chmod +x "$PATHS_SCRIPT" "$BODY_SCRIPT"

# Returns 0 if the gate would pass, 1 if it would fail. Same composition as
# the workflow: if any agent-input path → require either an eval-coverage
# path also touched OR a `[skip-eval-check]` body marker.
run_gate() {
  local changed="$1"
  local body="$2"

  local input_touches
  input_touches=$(printf '%s\n' "$changed" | "$PATHS_SCRIPT" eval-input || true)
  if [[ -z "$input_touches" ]]; then
    return 0  # No agent inputs touched — gate is a no-op pass.
  fi

  local coverage_touches
  coverage_touches=$(printf '%s\n' "$changed" | "$PATHS_SCRIPT" eval-coverage || true)
  if [[ -n "$coverage_touches" ]]; then
    return 0  # Fixture or eval suite also updated → pass.
  fi

  if printf '%s' "$body" | "$BODY_SCRIPT" \
        --required-regex '^__never_matches__$' \
        --skip-marker '[skip-eval-check]'; then
    return 0
  fi
  return 1
}

echo "T8 — require-eval-fixture gate"

start_case "no relevant paths touched → pass"
changed="README.md
src/apps/web/src/app/page.tsx"
body=""
run_gate "$changed" "$body" && actual="pass" || actual="fail"
assert_eq "pass" "$actual"

start_case "prompt touched + fixture touched → pass"
changed="src/apps/api/prompts/extract_text_overlays.txt
src/apps/api/tests/fixtures/agent_evals/template_text/ground_truth/rich_in_life_v2.json"
body=""
run_gate "$changed" "$body" && actual="pass" || actual="fail"
assert_eq "pass" "$actual"

start_case "prompt touched + evals/ touched → pass"
changed="src/apps/api/prompts/template_recipe.txt
src/apps/api/tests/evals/test_template_recipe_evals.py"
body=""
run_gate "$changed" "$body" && actual="pass" || actual="fail"
assert_eq "pass" "$actual"

start_case "prompt touched + nothing else + [skip-eval-check] with justification → pass"
changed="src/apps/api/prompts/extract_text_overlays.txt"
body="[skip-eval-check] regenerating fixtures offline in follow-up"
run_gate "$changed" "$body" && actual="pass" || actual="fail"
assert_eq "pass" "$actual"

start_case "prompt touched + nothing else → fail"
changed="src/apps/api/prompts/extract_text_overlays.txt"
body="Tweak phrasing."
run_gate "$changed" "$body" && actual="pass" || actual="fail"
assert_eq "fail" "$actual"

start_case "schema touched + nothing else → fail"
changed="src/apps/api/app/agents/_schemas/template_text.py"
body=""
run_gate "$changed" "$body" && actual="pass" || actual="fail"
assert_eq "fail" "$actual"

start_case "agent module touched + nothing else → fail"
changed="src/apps/api/app/agents/template_text.py"
body=""
run_gate "$changed" "$body" && actual="pass" || actual="fail"
assert_eq "fail" "$actual"

# Excludes: _runtime.py is infra under every agent — should NOT trigger T8.
start_case "_runtime.py only touched → pass (excluded from agent-input set)"
changed="src/apps/api/app/agents/_runtime.py"
body=""
run_gate "$changed" "$body" && actual="pass" || actual="fail"
assert_eq "pass" "$actual"

start_case "skip marker with <10 char justification → fail"
changed="src/apps/api/prompts/template_recipe.txt"
body="[skip-eval-check] short"
run_gate "$changed" "$body" && actual="pass" || actual="fail"
assert_eq "fail" "$actual"

summary
