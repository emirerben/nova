#!/usr/bin/env bash
# One-time, idempotent local setup for the agent-run Slack recap. This renders
# (but deliberately does not load) the launchd job so the operator can validate
# a manual run before scheduling it.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STATE_DIR="${NOVA_AGENT_RUNS_STATE_DIR:-$HOME/.nova/agent-runs}"
ENV_FILE="${NOVA_AGENT_RUNS_ENV_FILE:-$HOME/.nova/agent-runs.env}"
PLIST_SRC="$REPO_ROOT/infra/launchd/com.nova.agent-runs-recap.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.nova.agent-runs-recap.plist"
RECAP_SCRIPT="$REPO_ROOT/scripts/hooks/agent-runs-recap.sh"
LABEL="com.nova.agent-runs-recap"

mkdir -p "$STATE_DIR" "$HOME/Library/LaunchAgents" "$HOME/.nova/logs"
chmod 700 "$STATE_DIR"

if [ ! -f "$ENV_FILE" ]; then
  echo "[install] scaffolding $ENV_FILE (chmod 600; add the Slack webhook yourself)"
  (umask 077; cat >"$ENV_FILE" <<'ENVEOF'
# Local agent-run recap configuration. Keep this file private (chmod 600).
# REQUIRED: Slack incoming-webhook URL. Do not commit this value.
NOVA_AGENT_RUNS_WEBHOOK_URL=
ENVEOF
  )
fi
chmod 600 "$ENV_FILE"

if [ ! -f "$PLIST_SRC" ] || [ ! -x "$RECAP_SCRIPT" ]; then
  echo "[install] ERROR: recap template or script is missing from $REPO_ROOT" >&2
  exit 1
fi
sed -e "s|__AGENT_RUNS_RECAP__|$RECAP_SCRIPT|g" -e "s|__HOME__|$HOME|g" "$PLIST_SRC" >"$PLIST_DST"
chmod 644 "$PLIST_DST"
plutil -lint "$PLIST_DST" >/dev/null

cat <<NEXT
[install] installed local files but did NOT load the schedule.

1. Put your Slack incoming-webhook URL in: $ENV_FILE
2. Prove a manual run first:
     bash "$RECAP_SCRIPT"
3. After validation, enable weekdays at 06:00 local:
     launchctl bootstrap gui/\$(id -u) "$PLIST_DST"
   Disable it with:
     launchctl bootout gui/\$(id -u)/$LABEL

The recap posts metadata only (founder, tool, ticket, duration, outcome); it never sends
prompts, transcripts, command output, paths, or the completed-run ledger itself.
NEXT
