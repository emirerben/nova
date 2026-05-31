#!/usr/bin/env bash
# new-session.sh — create a fresh worktree branched off the current origin/main tip.
# This is the mandatory first step of any non-trivial session (see CLAUDE.md).
# Usage:  bash scripts/new-session.sh <topic>
# Example: bash scripts/new-session.sh template-text-100

set -euo pipefail

if [ $# -ne 1 ]; then
  echo "Usage: bash scripts/new-session.sh <topic>" >&2
  echo "  <topic> must be kebab-case, no slashes. Example: template-text-100" >&2
  exit 2
fi

topic="$1"
if ! [[ "$topic" =~ ^[a-z0-9][a-z0-9-]*$ ]]; then
  echo "ERROR: topic must be kebab-case (lowercase letters, digits, hyphens; no leading hyphen)." >&2
  echo "  Got: '$topic'" >&2
  exit 2
fi

repo_root="$(git rev-parse --show-toplevel 2>/dev/null)" || {
  echo "ERROR: not inside a git repository." >&2
  exit 2
}

parent="$(dirname "$repo_root")"
target_path="$parent/nova-$topic"
branch="feat/${topic}-$(date +%Y-%m-%d)"

if [ -e "$target_path" ]; then
  echo "ERROR: $target_path already exists. Pick a different topic or remove the old worktree first:" >&2
  echo "  git worktree remove $target_path" >&2
  exit 1
fi

if git show-ref --verify --quiet "refs/heads/$branch"; then
  echo "ERROR: branch '$branch' already exists. Pick a different topic." >&2
  exit 1
fi

echo "→ Fetching origin/main..."
git fetch origin main --quiet

echo "→ Creating worktree: $target_path"
echo "→ Branch:            $branch (off origin/main)"
git worktree add -b "$branch" "$target_path" origin/main

# Propagate the gbrain per-worktree pin (toplevel-scoped) so the new worktree
# gets semantic code search instead of silently falling back to grep.
if [ -f "$repo_root/.gbrain-source" ]; then
  cp "$repo_root/.gbrain-source" "$target_path/.gbrain-source" 2>/dev/null || true
  echo "→ Copied .gbrain-source pin to $target_path"
fi

cat <<EOF

Fresh worktree ready. Next step (run this yourself — scripts can't cd for you):

  cd $target_path

Then do your work there. When the PR merges:

  git worktree remove $target_path

EOF
