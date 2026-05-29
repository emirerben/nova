#!/usr/bin/env bash
# session-check.sh — fire on Claude Code SessionStart.
# Prints a warning to stdout (becomes additional context for the model) when
# the current checkout is behind origin/main. Exits 0 unconditionally — this
# is informational, never a gate. Failures (offline, no remote, not a git
# repo) are swallowed silently so they don't disrupt a session.

set -u

repo_root="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
cd "$repo_root" || exit 0

# Skip if no origin/main ref exists yet (fresh clone, weird remote setup).
git rev-parse --verify --quiet origin/main >/dev/null 2>&1 || exit 0

# Fetch with a hard 5s timeout; offline/slow networks must not block a session.
# --no-tags keeps the fetch lean. stderr suppressed; we only care about success/fail.
timeout 5 git fetch origin main --quiet --no-tags >/dev/null 2>&1 || true

branch="$(git branch --show-current 2>/dev/null || echo '')"
# Detached HEAD → no useful comparison.
[ -z "$branch" ] && exit 0

behind="$(git rev-list --count HEAD..origin/main 2>/dev/null || echo 0)"
[ "$behind" -eq 0 ] 2>/dev/null && exit 0

main_tip="$(git log -1 --format='%h %s' origin/main 2>/dev/null || echo 'unknown')"

if [ "$branch" = "main" ]; then
  # Clean tree → auto fast-forward to the freshly-fetched origin/main so the
  # shared checkout never drifts. Dirty tree or a diverged history → warn only
  # (never discard local work or rewrite history from a SessionStart hook).
  if [ -z "$(git status --porcelain 2>/dev/null)" ]; then
    if git merge --ff-only origin/main --quiet >/dev/null 2>&1; then
      echo "Auto-updated local main to origin/main ($main_tip)."
      exit 0
    fi
    cat <<EOF
WARNING: local main is $behind commit(s) behind origin/main and could NOT fast-forward (history diverged).
  Origin tip: $main_tip
  Resolve manually: git pull --rebase  (inspect with: git log --oneline main..origin/main)
EOF
    exit 0
  fi
  cat <<EOF
WARNING: local main is $behind commit(s) behind origin/main (working tree dirty — not auto-updating).
  Origin tip: $main_tip
  Commit or stash, then: git pull --ff-only
EOF
  exit 0
fi

cat <<EOF
WARNING: STALE WORKTREE — on branch '$branch', $behind commit(s) behind origin/main.
  Origin tip: $main_tip
  To start fresh work off main: bash scripts/new-session.sh <topic>
  To update this branch:        git merge --ff-only origin/main  (or rebase)
EOF
exit 0
