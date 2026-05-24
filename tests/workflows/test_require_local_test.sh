#!/usr/bin/env bash
#
# Tests for the T6 require-local-test gate. Exercises the two shared scripts
# (`check-touched-paths.sh` + `check-pr-body.sh`) the workflow composes, the
# same way the workflow composes them.
#
# Each case:
#   1. Pipes a fake "git diff --name-only" output through check-touched-paths.sh
#   2. If any paths matched, runs check-pr-body.sh against a fake PR body.
#   3. Asserts the resulting gate verdict matches expectations.

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
# shellcheck source=./_assert.sh
. "$HERE/_assert.sh"

PATHS_SCRIPT="$REPO/scripts/ci/check-touched-paths.sh"
BODY_SCRIPT="$REPO/scripts/ci/check-pr-body.sh"

chmod +x "$PATHS_SCRIPT" "$BODY_SCRIPT"

# Returns 0 if the gate would pass, 1 if it would fail. Mirrors the workflow's
# composition: detect touches → if any → require body marker.
run_gate() {
  local changed="$1"
  local body="$2"

  local matched
  matched=$(printf '%s\n' "$changed" | "$PATHS_SCRIPT" local-test || true)
  if [[ -z "$matched" ]]; then
    return 0  # No relevant touches — gate is a no-op pass.
  fi

  if printf '%s' "$body" | "$BODY_SCRIPT" \
        --required-regex '^Local test: [a-f0-9-]{6,}$' \
        --skip-marker '[skip-local-test]'; then
    return 0
  fi
  return 1
}

echo "T6 — require-local-test gate"

start_case "no relevant paths touched + empty PR body → pass"
changed="README.md
src/apps/web/src/app/page.tsx"
body=""
run_gate "$changed" "$body" && actual="pass" || actual="fail"
assert_eq "pass" "$actual"

start_case "relevant path touched + valid Local test marker → pass"
changed="src/apps/api/app/pipeline/text_overlay_v2/pipeline.py"
body="Refactors the OCR stage.

Local test: a1091488-abcd-4ef0-9876-1234567890ab
"
run_gate "$changed" "$body" && actual="pass" || actual="fail"
assert_eq "pass" "$actual"

start_case "relevant path touched + valid [skip-local-test] with long reason → pass"
changed="src/apps/api/app/agents/template_text.py"
body="[skip-local-test] cosmetic comment fix only"
run_gate "$changed" "$body" && actual="pass" || actual="fail"
assert_eq "pass" "$actual"

start_case "relevant path touched + missing marker → fail"
changed="src/apps/api/app/pipeline/text_overlay_v2/phrases.py"
body="Just a refactor."
run_gate "$changed" "$body" && actual="pass" || actual="fail"
assert_eq "fail" "$actual"

start_case "relevant path touched + [skip-local-test] with <10 char justification → fail"
changed="src/apps/api/app/agents/text_alignment.py"
body="[skip-local-test] short"
run_gate "$changed" "$body" && actual="pass" || actual="fail"
assert_eq "fail" "$actual"

# Extra coverage: each of the 6 path patterns matches at least one realistic file.
start_case "agentic_template_build.py also triggers the gate"
changed="src/apps/api/app/tasks/agentic_template_build.py"
body=""
run_gate "$changed" "$body" && actual="pass" || actual="fail"
assert_eq "fail" "$actual"

start_case "template_orchestrate.py also triggers the gate"
changed="src/apps/api/app/tasks/template_orchestrate.py"
body=""
run_gate "$changed" "$body" && actual="pass" || actual="fail"
assert_eq "fail" "$actual"

# Defense-in-depth: Local test marker requires `^...$` shape — a trailing word
# would historically have been a problem if we'd let grep match a prefix.
start_case "Local test marker with trailing junk → fail"
changed="src/apps/api/app/pipeline/text_overlay_v2/pipeline.py"
body="Local test: a1091488-abcd see other PR"
run_gate "$changed" "$body" && actual="pass" || actual="fail"
assert_eq "fail" "$actual"

# CRLF tolerance: GitHub web UI submits PR bodies with CRLF line endings.
start_case "Local test marker with CRLF line endings → pass"
changed="src/apps/api/app/pipeline/text_overlay_v2/pipeline.py"
body=$'Refactor.\r\nLocal test: deadbeef1234\r\n'
run_gate "$changed" "$body" && actual="pass" || actual="fail"
assert_eq "pass" "$actual"

summary
