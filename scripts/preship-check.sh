#!/usr/bin/env bash
# preship-check.sh — mechanical pre-ship gate. Runs the checks that have each
# cost an extra CI/commit cycle when forgotten (see the shipping rules in
# agents/DECISIONS.md and session memory):
#
#   1. scoped ruff (format + lint) on changed .py files only
#   2. tsc --noEmit when any web .ts/.tsx changed
#   3. drift check: files in this branch that ALSO changed on origin/main
#   4. VERSION slot check vs origin/main (bumped, and not a collision)
#   5. list the [skip-*] CI markers relevant to this diff
#
# Usage: bash scripts/preship-check.sh   (from the feature worktree, pre-PR)
# Exit 0 = ship; non-zero = fix the FAILs first.

set -uo pipefail

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
echo "[1/5] scoped ruff"
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

# ── 2. tsc when web TS changed ────────────────────────────────────────────────
echo "[2/5] tsc --noEmit"
if echo "$CHANGED" | grep -qE '^src/apps/web/.*\.tsx?$'; then
  if (cd src/apps/web && npx tsc --noEmit) >/dev/null 2>&1; then
    pass "web typecheck clean"
  else
    fail "tsc --noEmit failed — run: (cd src/apps/web && npx tsc --noEmit)"
  fi
else
  info "no web .ts/.tsx changes — skipped"
fi

# ── 3. Drift: my files that also changed on origin/main since branch point ───
echo "[3/5] file drift vs origin/main"
MAIN_CHANGED="$(git diff --name-only "$MB" origin/main | sort -u)"
DRIFT="$(comm -12 <(echo "$CHANGED") <(echo "$MAIN_CHANGED") | grep -vE '^(VERSION|CHANGELOG\.md)$' || true)"
if [ -n "$DRIFT" ]; then
  warn "these files also changed on origin/main since your branch point — rebase before merging:"
  echo "$DRIFT" | sed 's/^/         /'
else
  pass "no overlapping changes with origin/main"
fi

# ── 4. VERSION slot ───────────────────────────────────────────────────────────
echo "[4/5] VERSION slot"
LOCAL_V="$(cat VERSION 2>/dev/null | tr -d '[:space:]')"
MAIN_V="$(git show origin/main:VERSION 2>/dev/null | tr -d '[:space:]')"
if [ -z "$LOCAL_V" ] || [ -z "$MAIN_V" ]; then
  warn "could not read VERSION locally or on origin/main"
elif [ "$LOCAL_V" = "$MAIN_V" ]; then
  fail "VERSION not bumped (still $MAIN_V) — bump past origin/main before shipping"
elif [ "$(printf '%s\n%s\n' "$LOCAL_V" "$MAIN_V" | sort -V | tail -1)" = "$MAIN_V" ]; then
  fail "VERSION slot collision: local $LOCAL_V <= origin/main $MAIN_V — origin moved; pick the next free slot"
else
  pass "VERSION $MAIN_V -> $LOCAL_V"
fi

# ── 5. CI skip-markers relevant to this diff ──────────────────────────────────
echo "[5/5] CI [skip-*] markers"
MARKERS="$(grep -rhoE '\[skip-[a-z0-9-]+\]' .github/workflows/ 2>/dev/null | sort -u)"
if [ -n "$MARKERS" ]; then
  info "markers honored by CI (add to PR body ONLY with a reason):"
  echo "$MARKERS" | sed 's/^/         /'
else
  info "none found"
fi

echo
if [ "$FAILS" -gt 0 ]; then
  echo "preship-check: $FAILS FAIL(s) — fix before opening the PR."
  exit 1
fi
echo "preship-check: all green."
