#!/usr/bin/env bash
# check_claude_md_size.sh
#
# CI guard: fail if CLAUDE.md exceeds the 38,000-char budget.
#
# Why: Claude Code warns and degrades when CLAUDE.md is large. 38k gives a
# comfortable buffer below the harness warning. When the guard fires, the output
# lists every section sorted by size so the trim target is obvious.
#
# Pattern:
#   KEEP inline: invariants, guard-test names, file pointers, commands.
#   MOVE out: incident narratives (prod job IDs, multi-paragraph "why") →
#             agents/DECISIONS.md; feature internals → docs/pipelines/ or
#             docs/runbooks/. Same pattern as commit ebc4413b (#329).
#
# Escape hatch: [skip-claude-md-size-check] in any PR commit message.
#
# Local use: bash scripts/check_claude_md_size.sh

set -euo pipefail

TARGET_FILE="CLAUDE.md"
BUDGET=38000

if [ ! -f "$TARGET_FILE" ]; then
  echo "check_claude_md_size: $TARGET_FILE not found — skipping."
  exit 0
fi

# Escape hatch: any commit on the PR opts out.
if [ -n "${BASE_SHA:-}" ] && [ -n "${HEAD_SHA:-}" ]; then
  commit_msgs="$(git log --format='%B' "${BASE_SHA}..${HEAD_SHA}" 2>/dev/null || true)"
  if printf '%s' "$commit_msgs" | grep -qF '[skip-claude-md-size-check]'; then
    echo "check_claude_md_size: [skip-claude-md-size-check] found — bypassing."
    exit 0
  fi
fi

size="$(wc -c < "$TARGET_FILE" | tr -d ' ')"

if [ "$size" -le "$BUDGET" ]; then
  echo "check_claude_md_size: OK — ${size} chars (budget ${BUDGET})."
  exit 0
fi

# Fail: print section breakdown so the trim target is obvious.
cat >&2 <<EOF
check_claude_md_size: FAIL

CLAUDE.md is ${size} chars — $(( size - BUDGET )) over the ${BUDGET}-char budget.

Section breakdown (sorted largest first):
EOF

# Extract ## sections and measure each block.
awk '
  /^## / {
    if (section != "") { print length(buf) "\t" section }
    section = $0; buf = ""
  }
  { buf = buf $0 "\n" }
  END { if (section != "") print length(buf) "\t" section }
' "$TARGET_FILE" | sort -rn | head -20 | while IFS=$'\t' read -r chars heading; do
  printf '  %6d  %s\n' "$chars" "$heading"
done >&2

cat >&2 <<EOF

Fix: move incident narratives to agents/DECISIONS.md, feature internals to
docs/pipelines/ or docs/runbooks/. Keep inline: invariants, guard-test names,
file pointers, commands. See CLAUDE.md "## CLAUDE.md size budget" for the policy.
EOF
exit 1
