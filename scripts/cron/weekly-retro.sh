#!/usr/bin/env bash
# Weekly engineering retro, driven by launchd (Friday 17:00 local,
# com.nova.weekly-retro). Same headless pattern as research-tiktok-weekly.sh.
#
# Runs the gstack /retro skill against the primary checkout: it mines the
# week's sessions/PRs for corrections, wasted time, and repeated manual steps,
# and writes the retro + learnings to ~/.gstack. Read it Monday morning.
#
# Install:  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.nova.weekly-retro.plist
# Disable:  launchctl bootout gui/$(id -u)/com.nova.weekly-retro
# Test now: bash scripts/cron/weekly-retro.sh

set -uo pipefail

export HOME="/Users/emirerben"
export PATH="$HOME/.bun/bin:$HOME/.local/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

REPO="$HOME/Projects/nova"
LOG_DIR="$HOME/.nova/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/weekly-retro-$(date +%Y%m%d-%H%M%S).log"
exec >>"$LOG" 2>&1
echo "=== weekly-retro start $(date) ==="

CLAUDE_BIN="$(command -v claude || true)"
if [ -z "$CLAUDE_BIN" ]; then
  echo "ERROR: claude CLI not found on PATH ($PATH). Aborting." >&2
  exit 1
fi

cd "$REPO" || { echo "ERROR: repo $REPO missing"; exit 1; }

# Headless, unattended run on the user's own machine (same rationale as
# research-tiktok-weekly.sh). /retro is read+report — it writes only to
# ~/.gstack, never to the repo.
"$CLAUDE_BIN" --print \
  --permission-mode bypassPermissions \
  --model claude-sonnet-4-6 \
  "/retro"
STATUS=$?

echo "=== weekly-retro end $(date) exit=$STATUS ==="
exit $STATUS
