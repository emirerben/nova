#!/usr/bin/env bash
# preship-check.sh — mechanical pre-ship gate. Runs the checks that have each
# cost an extra CI/commit cycle when forgotten (see the shipping rules in
# agents/DECISIONS.md and session memory):
#
#   1. scoped ruff (format + lint) on changed .py files only
#   2. drift check: files in this branch that ALSO changed on origin/main
#   3. release-metadata ownership check (post-merge automation owns versions)
#   4. list the [skip-*] CI markers relevant to this diff
#   5. run the KRI-577 reliability gates mapped to the changed incident paths
#   6. journey-mapping check: the same fail-closed mapping CI enforces, so an
#      unmapped path is named here instead of discovered in CI (KRI-749)
#   7. request-to-output evidence: validate the same PR-body contract as CI,
#      including staged, unstaged and untracked changes
#   8. fix-chain warning: a 3rd fix PR on one KRI ticket needs a failure-mode
#      list first (KRI-751; docs/runbooks/linear-workflow.md)
#
# Usage: bash scripts/preship-check.sh [--pr-body /path/to/pr-body.md]
# Default body: PR_EVIDENCE_BODY or .dev/pr-body.md (from the feature worktree).
# Exit 0 = ship; non-zero = fix the FAILs first.

set -uo pipefail

PR_BODY_FILE="${PR_EVIDENCE_BODY:-.dev/pr-body.md}"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --pr-body)
      [ "$#" -ge 2 ] || { echo "--pr-body requires a file path" >&2; exit 2; }
      PR_BODY_FILE="$2"
      shift 2
      ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
# Resolve caller-relative paths before changing to the repository root.
if [[ "$PR_BODY_FILE" != /* ]]; then
  PR_BODY_FILE="$PWD/$PR_BODY_FILE"
fi

REPO="$(git rev-parse --show-toplevel 2>/dev/null)" || { echo "not a git repo" >&2; exit 2; }
cd "$REPO"

FAILS=0
pass() { printf '  \033[32mPASS\033[0m %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAILS=$((FAILS + 1)); }
warn() { printf '  \033[33mWARN\033[0m %s\n' "$*"; }
info() { printf '  %s\n' "$*"; }

git fetch origin main --quiet 2>/dev/null || warn "could not fetch origin/main — comparing against last-known ref"
MB="$(git merge-base HEAD origin/main)"

# Changed files: committed since branch point + staged + unstaged + untracked.
CHANGED="$( { git diff --name-only "$MB" HEAD; git diff --name-only HEAD; git diff --name-only --cached; git ls-files --others --exclude-standard; } | sort -u )"

echo "preship-check vs origin/main ($(git rev-parse --short origin/main)), branch point $(git rev-parse --short "$MB")"

# ── 1. Scoped ruff on changed API .py files ───────────────────────────────────
echo "[1/8] scoped ruff"
PY_CHANGED="$(echo "$CHANGED" | grep -E '^src/apps/api/.*\.py$' || true)"
if [ -z "$PY_CHANGED" ]; then
  info "no API .py changes — skipped"
else
  REL="$(echo "$PY_CHANGED" | sed 's|^src/apps/api/||' | while read -r f; do [ -f "src/apps/api/$f" ] && echo "$f"; done)"
  if [ -z "$REL" ]; then
    info "all changed .py files deleted — skipped"
  else
    RUFF="$REPO/src/apps/api/.venv/bin/ruff"
    [ -x "$RUFF" ] || RUFF="$(command -v ruff || true)"
    if [ -z "$RUFF" ]; then
      warn "ruff not found (no venv, not on PATH) — skipped"
    else
      # shellcheck disable=SC2086
      if OUT="$( (cd src/apps/api && "$RUFF" check $REL && "$RUFF" format --check $REL) 2>&1 )"; then
        pass "ruff clean on $(echo "$REL" | wc -l | tr -d ' ') changed file(s)"
      else
        fail "ruff violations on changed files:"
        echo "$OUT" | sed 's/^/         /'
      fi
    fi
  fi
fi

# ── 2. Drift: my files that also changed on origin/main since branch point ───
echo "[2/8] file drift vs origin/main"
MAIN_CHANGED="$(git diff --name-only "$MB" origin/main | sort -u)"
DRIFT="$(comm -12 <(echo "$CHANGED") <(echo "$MAIN_CHANGED") | grep -vE '^(VERSION|CHANGELOG\.md)$' || true)"
if [ -n "$DRIFT" ]; then
  warn "these files also changed on origin/main since your branch point — rebase before merging:"
  echo "$DRIFT" | sed 's/^/         /'
else
  pass "no overlapping changes with origin/main"
fi

# ── 3. Release metadata ownership ─────────────────────────────────────────────
echo "[3/8] release metadata ownership"
if OUT="$(python3 scripts/release_metadata.py guard --base "$MB" --head HEAD 2>&1)" \
  && OUT_WORKING="$(python3 scripts/release_metadata.py guard --base HEAD --working 2>&1)"; then
  pass "feature PR leaves release metadata to post-merge automation"
else
  fail "${OUT:-$OUT_WORKING}"
fi

# ── 4. CI skip-markers relevant to this diff ──────────────────────────────────
echo "[4/8] CI [skip-*] markers"
MARKERS="$(grep -rhoE '\[skip-[a-z0-9-]+\]' .github/workflows/ 2>/dev/null | sort -u)"
if [ -n "$MARKERS" ]; then
  info "markers honored by CI (add to PR body ONLY with a reason):"
  echo "$MARKERS" | sed 's/^/         /'
else
  info "none found"
fi

# ── 5. Reliability gates for changed incident paths (KRI-577) ────────────────
echo "[5/8] reliability gates"
if [ -f "$REPO/.dev/test-db-url" ] && [ -z "${DATABASE_URL:-}" ]; then
  DATABASE_URL="$(cat "$REPO/.dev/test-db-url")"; export DATABASE_URL
fi
GATES="$(python3 scripts/reliability-gates.py select --base origin/main 2>&1)"
if echo "$GATES" | grep -q '^no reliability gate'; then
  info "no incident gate maps to the changed paths"
else
  echo "$GATES" | grep ' <- ' | sed 's/^/         /'
  if OUT="$(python3 scripts/reliability-gates.py run --changed --base origin/main 2>&1)"; then
    pass "mapped incident gates green ($(echo "$OUT" | grep -o '"passed": [0-9]*' | head -1))"
    echo "$OUT" | grep -q '^environment:' && warn "some DB-backed gates skipped — run: bash scripts/dev/test-db.sh"
  else
    fail "a mapped incident gate failed — the original incident or a neighbour regressed:"
    echo "$OUT" | tail -15 | sed 's/^/         /'
  fi
fi

# ── 6. Journey mapping (KRI-749) ──────────────────────────────────────────────
# Runs the CI selector's journey logic locally. It diffs COMMITTED history from
# the merge-base (exactly like CI), so commit before trusting this step. It
# checks the mapping only; journey api tests / xctests still run in CI.
echo "[6/8] journey mapping"
SELECT="scripts/ci/select-tests.py"
if [ ! -f "$SELECT" ]; then
  info "no $SELECT — skipped"
elif [ -z "$(git diff --name-only "$MB" HEAD)" ]; then
  info "no committed changes vs origin/main — skipped (commit first; CI diffs commits only)"
else
  if SEL="$(env -u GITHUB_OUTPUT -u GITHUB_STEP_SUMMARY CI_EVENT=pull_request CI_BASE="$MB" CI_HEAD=HEAD python3 "$SELECT" 2>&1)"; then
    J_SEL="$(echo "$SEL" | sed -n 's/^journey=//p' | head -1)"
    J_COV="$(echo "$SEL" | sed -n 's/^journey_coverage=//p' | head -1)"
    J_IDS="$(echo "$SEL" | sed -n 's/^journey_ids=//p' | head -1)"
    if [ "$J_SEL" = "false" ]; then
      pass "no journey-affecting paths in this diff"
    elif OUT="$(COVERAGE="$J_COV" JOURNEY_IDS="$J_IDS" python3 "$SELECT" validate-mapping 2>&1)"; then
      pass "every journey-relevant path is mapped; CI will run $(echo "$J_IDS" | tr ',' '\n' | grep -c .) journey(s)"
    else
      fail "journey mapping would fail CI (coverage=$J_COV) — unmapped path(s):"
      UNMAPPED="$(python3 "$SELECT" unmapped-paths --base "$MB" --head HEAD 2>&1)"
      if [ -n "$UNMAPPED" ]; then
        echo "$UNMAPPED" | sed 's/^/         /'
      else
        echo "         (selector reported a gap but no unmapped path — empty diff or git error)"
      fi
      info "fix: add each path to a journey in scripts/ci/journey-manifest.json"
    fi
    info "limit: mapping only — journey api tests / xctests run in CI"
  else
    fail "journey selector crashed:"
    echo "$SEL" | tail -8 | sed 's/^/         /'
  fi
  # Uncommitted files are invisible to the selector; flag the ones that would
  # be unmapped once committed.
  COMMITTED="$(git diff --name-only "$MB" HEAD | sort -u)"
  UNCOMMITTED="$(comm -23 <(echo "$CHANGED") <(echo "$COMMITTED"))"
  if [ -n "$UNCOMMITTED" ]; then
    UNCOMMITTED_UNMAPPED="$(echo "$UNCOMMITTED" | python3 "$SELECT" unmapped-paths --stdin 2>/dev/null)"
    if [ -n "$UNCOMMITTED_UNMAPPED" ]; then
      warn "uncommitted file(s) are journey-relevant but unmapped (selector sees committed history only):"
      echo "$UNCOMMITTED_UNMAPPED" | sed 's/^/         /'
    fi
  fi
fi

# ── 7. Request-to-output evidence (KRI-577 / KRI-559) ───────────────────────
echo "[7/8] request-to-output evidence"
if OUT="$(python3 scripts/ci/pr-evidence.py --repo "$REPO" --base "$MB" \
  --head HEAD --working --body-file "$PR_BODY_FILE" 2>&1)"; then
  pass "$OUT"
else
  fail "request-to-output evidence is missing or invalid:"
  echo "$OUT" | sed 's/^/         /'
  info "write the PR description to $PR_BODY_FILE or pass --pr-body /path/to/body.md"
  info "contract: docs/runbooks/request-to-output-standard.md"
fi

# ── 8. Fix-chain warning (KRI-751) ───────────────────────────────────────────
# Offline: counts fix-subject commits already on origin/main for this ticket.
echo "[8/8] fix-chain check"
BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
TICKET="$(echo "$BRANCH" | grep -oiE 'kri-[0-9]+' | head -1 | tr 'a-z' 'A-Z')"
if [ -z "$TICKET" ]; then
  info "no KRI-<n> in branch name '$BRANCH' — skipped"
else
  # -i grep, then an explicit digit boundary (git's regex has no portable \b).
  FIXES="$(git log origin/main --format=%s -i --grep="$TICKET" 2>/dev/null \
    | grep -iE "$TICKET([^0-9]|$)" | grep -icE '^fix' || true)"
  FIXES="${FIXES:-0}"
  if [ "$FIXES" -ge 2 ]; then
    warn "$TICKET already has $FIXES fix PR(s) on main — this would be fix #$((FIXES + 1)). Write the failure-mode list first (each mode: covered by test X, or 'uncovered - adding test X'): docs/runbooks/linear-workflow.md#fix-chains-the-second-fix-pr-rule"
  else
    pass "$TICKET has $FIXES fix PR(s) on main — no fix chain"
  fi
fi

echo
if [ "$FAILS" -gt 0 ]; then
  echo "preship-check: $FAILS FAIL(s) — fix before opening the PR."
  exit 1
fi
echo "preship-check: all green."
