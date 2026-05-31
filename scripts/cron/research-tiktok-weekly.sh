#!/usr/bin/env bash
# Weekly TikTok market-research run, driven by launchd (Sunday 20:00 local).
# launchd starts jobs with a bare environment, so we set PATH explicitly and
# resolve every binary ourselves. Logs to ~/.nova/logs/research-tiktok-*.log.
#
# Activated by ~/Library/LaunchAgents/com.nova.research-tiktok.plist.
# Disable temporarily:  launchctl unload ~/Library/LaunchAgents/com.nova.research-tiktok.plist
# Re-enable:            launchctl load   ~/Library/LaunchAgents/com.nova.research-tiktok.plist
# Run once now (test):  bash ~/Projects/nova/scripts/cron/research-tiktok-weekly.sh

set -uo pipefail

# --- environment (launchd has none of this) --------------------------------
export HOME="/Users/emirerben"
export PATH="$HOME/.bun/bin:$HOME/.local/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

REPO="$HOME/Projects/nova"
LOG_DIR="$HOME/.nova/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/research-tiktok-$(date +%Y%m%d-%H%M%S).log"

exec >>"$LOG" 2>&1
echo "=== research-tiktok-weekly start $(date) ==="

CLAUDE_BIN="$(command -v claude || true)"
if [ -z "$CLAUDE_BIN" ]; then
  echo "ERROR: claude CLI not found on PATH ($PATH). Aborting." >&2
  exit 1
fi
echo "claude: $CLAUDE_BIN"
command -v yt-dlp >/dev/null 2>&1 || echo "WARN: yt-dlp not on PATH — research-tiktok fetch may fail."

cd "$REPO" || { echo "ERROR: repo $REPO missing"; exit 1; }

# Keep the shared checkout fresh so the skill branches off a current main.
git fetch origin main --quiet 2>/dev/null || true

# Headless agent run. --permission-mode bypassPermissions because this is an
# unattended job on the user's own machine (it must git push + open a PR with
# no human to approve prompts). Edit CLAUDE_PROMPT/flags here if the skill name
# or model changes.
CLAUDE_PROMPT="/research-tiktok"
"$CLAUDE_BIN" --print \
  --permission-mode bypassPermissions \
  --model claude-sonnet-4-6 \
  "$CLAUDE_PROMPT"
STATUS=$?

echo "=== research-tiktok-weekly end $(date) exit=$STATUS ==="
exit $STATUS
