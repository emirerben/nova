#!/usr/bin/env bash
#
# check-touched-paths.sh — pure path-matching helper for CI pre-merge gates.
#
# Reads a newline-separated list of changed paths on stdin (one path per line —
# the same shape `git diff --name-only base...HEAD` produces). Emits any path
# that matches the named category to stdout, one per line. Exits 0 always
# (matching is a question, not a verdict).
#
# Categories:
#   local-test    — paths whose change must be accompanied by a `Local test:`
#                   line in the PR body. Lane C / T6.
#   eval-input    — paths whose change must be accompanied by an eval fixture
#                   or eval-suite change in the same PR. Lane C / T8.
#   eval-coverage — paths that count as a satisfying eval-fixture or eval-suite
#                   update for the eval-input gate.
#
# Usage:
#   git diff --name-only "$BASE...HEAD" | scripts/ci/check-touched-paths.sh local-test
#   printf '%s\n' a.py b.py | scripts/ci/check-touched-paths.sh eval-input
#
# Wildcards are matched with bash's `==` glob inside `[[ ]]`, with `**`
# expanded to `*` so the path-list reads like the workflow YAML's `paths:`
# block. Comparisons are anchored: we match against the full repo-relative
# path, never a substring.

set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 <category>" >&2
  echo "  category: local-test | eval-input | eval-coverage" >&2
  exit 2
fi

category="$1"

# T6 — Layer-2 pipeline + the agents/tasks that drive it.
# Listed here (not in the YAML) so the workflow and the test harness share
# one source of truth.
local_test_globs=(
  "src/apps/api/app/pipeline/text_overlay_v2/**"
  "src/apps/api/app/agents/template_text.py"
  "src/apps/api/app/agents/text_alignment.py"
  "src/apps/api/app/agents/text_classification.py"
  "src/apps/api/app/tasks/agentic_template_build.py"
  "src/apps/api/app/tasks/template_orchestrate.py"
)

# T8 — prompt + agent + schema changes that alter agent inputs/outputs.
# `_runtime.py` is excluded because it's the infra under every agent, not a
# per-agent input change. Adding it here would force fixture updates for every
# observability tweak.
eval_input_globs=(
  "src/apps/api/prompts/**"
  "src/apps/api/app/agents/*.py"
  "src/apps/api/app/agents/_schemas/*.py"
)

eval_input_excludes=(
  "src/apps/api/app/agents/_runtime.py"
)

# T8 — what counts as a fixture/eval update that satisfies the gate.
eval_coverage_globs=(
  "src/apps/api/tests/fixtures/agent_evals/**"
  "src/apps/api/tests/evals/**"
)

# Bash-glob match with `**` collapsed to `*` so caller-friendly globs work.
# `[[ $path == $pattern ]]` is a glob match (not a regex), which is exactly
# what GitHub Actions' `paths:` filter uses semantically.
_match_glob() {
  local path="$1"
  local pattern="$2"
  # Collapse `**` → `*`. Bash's `${var//<from>/<to>}` does NOT treat backslash
  # as an escape, so we write the `*`s plain — using `\*` here would put a
  # literal backslash into the result and break `[[ == ]]` matching.
  local expanded="${pattern//\*\*/*}"
  # shellcheck disable=SC2053  # intentional unquoted RHS for glob match
  [[ $path == $expanded ]]
}

_any_match() {
  local path="$1"
  shift
  local pat
  for pat in "$@"; do
    if _match_glob "$path" "$pat"; then
      return 0
    fi
  done
  return 1
}

case "$category" in
  local-test)
    while IFS= read -r path; do
      [[ -z "$path" ]] && continue
      if _any_match "$path" "${local_test_globs[@]}"; then
        printf '%s\n' "$path"
      fi
    done
    ;;
  eval-input)
    while IFS= read -r path; do
      [[ -z "$path" ]] && continue
      # Excludes first — `_runtime.py` matches the agents/*.py glob.
      if _any_match "$path" "${eval_input_excludes[@]}"; then
        continue
      fi
      if _any_match "$path" "${eval_input_globs[@]}"; then
        printf '%s\n' "$path"
      fi
    done
    ;;
  eval-coverage)
    while IFS= read -r path; do
      [[ -z "$path" ]] && continue
      if _any_match "$path" "${eval_coverage_globs[@]}"; then
        printf '%s\n' "$path"
      fi
    done
    ;;
  *)
    echo "unknown category: $category" >&2
    echo "  expected: local-test | eval-input | eval-coverage" >&2
    exit 2
    ;;
esac
