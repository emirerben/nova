#!/usr/bin/env bash
# agent-runs.sh — fail-open, privacy-minimal lifecycle reporting for local agents.
#
# Hook usage:
#   bash scripts/hooks/agent-runs.sh start codex
#   bash scripts/hooks/agent-runs.sh finish claude
#
# The hook JSON arrives on stdin. Prompt content is examined in memory solely to
# find an initial KRI-xx key; it is never written to disk or sent to Slack.

set -uo pipefail

ACTION="${1:-}"
TOOL="${2:-}"
case "$ACTION:$TOOL" in
  start:codex|finish:codex|start:claude|finish:claude) ;;
  *) exit 0 ;;
esac

INPUT="$(cat 2>/dev/null || true)"
[ -n "$INPUT" ] || exit 0

# Print a unit-separator-delimited session id, initial-prompt ticket, and normalized
# outcome. The prompt itself deliberately never leaves this short-lived process.
PARSED="$(printf '%s' "$INPUT" | python3 -c '
import json
import re
import sys

try:
    payload = json.load(sys.stdin)
except (json.JSONDecodeError, TypeError):
    raise SystemExit(0)

def first(*keys):
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""

session = first("session_id", "sessionId", "conversation_id", "conversationId", "thread_id", "threadId", "run_id", "runId")
prompt = first("prompt", "user_prompt", "userPrompt", "message")
if not prompt and isinstance(payload.get("tool_input"), dict):
    prompt = payload["tool_input"].get("prompt", "")
ticket_match = re.search(r"\b(KRI-\d+)\b", prompt, re.IGNORECASE)
ticket = ticket_match.group(1).upper() if ticket_match else ""
raw_outcome = first("outcome", "status", "stop_reason", "stopReason", "reason").lower()
if raw_outcome in {"failed", "failure", "error"}:
    outcome = "failed"
elif raw_outcome in {"cancelled", "canceled", "aborted", "interrupted", "timeout"}:
    outcome = "interrupted"
else:
    outcome = "completed"

if session:
    print(f"{session}\x1f{ticket}\x1f{outcome}")
' 2>/dev/null || true)"
[ -n "$PARSED" ] || exit 0
IFS=$'\x1f' read -r SESSION_ID TICKET OUTCOME <<EOF
$PARSED
EOF
[ -n "$SESSION_ID" ] || exit 0

STATE_DIR="${NOVA_AGENT_RUNS_STATE_DIR:-$HOME/.nova/agent-runs}"
ENV_FILE="${NOVA_AGENT_RUNS_ENV_FILE:-$HOME/.nova/agent-runs.env}"

load_webhook() {
  [ -f "$ENV_FILE" ] && [ -O "$ENV_FILE" ] && [ -r "$ENV_FILE" ] || return 1
  # This is user-owned local configuration, created by install-agent-runs.sh.
  # shellcheck disable=SC1090
  source "$ENV_FILE" >/dev/null 2>&1 || return 1
  [ -n "${NOVA_AGENT_RUNS_WEBHOOK_URL:-}" ]
}

post_json() {
  local payload="$1"
  load_webhook || return 1
  printf '%s' "$payload" | curl --fail --silent --show-error --max-time 5 \
    -H 'Content-Type: application/json' --data-binary @- \
    "$NOVA_AGENT_RUNS_WEBHOOK_URL" >/dev/null 2>&1
}

state_key="$(printf '%s' "$TOOL:$SESSION_ID" | shasum -a 256 2>/dev/null | awk '{print $1}')"
[ -n "$state_key" ] || exit 0
STATE_FILE="$STATE_DIR/$state_key.json"

mkdir -p "$STATE_DIR" 2>/dev/null || exit 0
chmod 700 "$STATE_DIR" 2>/dev/null || true

if [ "$ACTION" = "start" ]; then
  # A state file means this session has already received its initial prompt.
  [ -e "$STATE_FILE" ] && exit 0
  LOCK_DIR="$STATE_DIR/.start-$state_key.lock"
  mkdir "$LOCK_DIR" 2>/dev/null || exit 0
  trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT
  [ -e "$STATE_FILE" ] && exit 0

  FOUNDER="${NOVA_AGENT_RUNS_FOUNDER:-$(git config user.name 2>/dev/null || true)}"
  [ -n "$FOUNDER" ] || FOUNDER="unknown"
  STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  STARTED_EPOCH="$(date +%s)"
  TMP_FILE="$(mktemp "$STATE_DIR/.run.XXXXXX" 2>/dev/null || true)"
  [ -n "$TMP_FILE" ] || exit 0
  umask 077
  if [ -z "$TICKET" ]; then
    TOOL="$TOOL" SESSION_ID="$SESSION_ID" python3 -c '
import json, os, sys
json.dump({"tool": os.environ["TOOL"], "session_id": os.environ["SESSION_ID"], "tracked": False}, sys.stdout, separators=(",", ":"))
' >"$TMP_FILE" 2>/dev/null || { rm -f "$TMP_FILE"; exit 0; }
    chmod 600 "$TMP_FILE" 2>/dev/null || true
    mv "$TMP_FILE" "$STATE_FILE" 2>/dev/null || true
    exit 0
  fi
  FOUNDER="$FOUNDER" TOOL="$TOOL" TICKET="$TICKET" SESSION_ID="$SESSION_ID" STARTED_AT="$STARTED_AT" STARTED_EPOCH="$STARTED_EPOCH" \
    python3 -c '
import json, os, sys
json.dump({
    "founder": os.environ["FOUNDER"],
    "tool": os.environ["TOOL"],
    "ticket": os.environ["TICKET"],
    "session_id": os.environ["SESSION_ID"],
    "started_at": os.environ["STARTED_AT"],
    "started_epoch": int(os.environ["STARTED_EPOCH"]),
    "tracked": True,
}, sys.stdout, separators=(",", ":"))
' >"$TMP_FILE" 2>/dev/null || { rm -f "$TMP_FILE"; exit 0; }
  chmod 600 "$TMP_FILE" 2>/dev/null || true
  mv "$TMP_FILE" "$STATE_FILE" 2>/dev/null || exit 0

  PAYLOAD="$(FOUNDER="$FOUNDER" TOOL="$TOOL" TICKET="$TICKET" STARTED_AT="$STARTED_AT" python3 -c '
import json, os
text = "Agent run started | founder={founder} | tool={tool} | ticket={ticket} | started_at={started_at}".format(
    founder=os.environ["FOUNDER"], tool=os.environ["TOOL"], ticket=os.environ["TICKET"], started_at=os.environ["STARTED_AT"]
)
print(json.dumps({"text": text}, separators=(",", ":")))
' 2>/dev/null || true)"
  [ -n "$PAYLOAD" ] && post_json "$PAYLOAD" || true
  exit 0
fi

[ -r "$STATE_FILE" ] || exit 0
LOCK_DIR="$STATE_DIR/.finish-$state_key.lock"
mkdir "$LOCK_DIR" 2>/dev/null || exit 0
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT
[ -r "$STATE_FILE" ] || exit 0
TRACKED="$(python3 -c '
import json, sys
try:
    print("yes" if json.load(open(sys.argv[1])).get("tracked", True) else "no")
except (OSError, ValueError, TypeError):
    pass
' "$STATE_FILE" 2>/dev/null || true)"
if [ "$TRACKED" = "no" ]; then
  rm -f "$STATE_FILE" 2>/dev/null || true
  exit 0
fi

STATE="$(python3 -c '
import json, sys
try:
    state = json.load(open(sys.argv[1]))
    required = ("founder", "tool", "ticket", "started_at", "started_epoch")
    if all(key in state for key in required):
        print(json.dumps(state, separators=(",", ":")))
except (OSError, ValueError, TypeError):
    pass
' "$STATE_FILE" 2>/dev/null || true)"
[ -n "$STATE" ] || exit 0

NOW_EPOCH="$(date +%s)"
FINISHED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
FINISH_FIELDS="$(STATE="$STATE" NOW_EPOCH="$NOW_EPOCH" FINISHED_AT="$FINISHED_AT" OUTCOME="$OUTCOME" python3 -c '
import json, os
state = json.loads(os.environ["STATE"])
elapsed = max(0, int(os.environ["NOW_EPOCH"]) - int(state["started_epoch"]))
record = {
    "founder": state["founder"], "tool": state["tool"], "ticket": state["ticket"],
    "started_at": state["started_at"], "finished_at": os.environ["FINISHED_AT"],
    "elapsed_seconds": elapsed, "outcome": os.environ["OUTCOME"],
}
print(json.dumps(record, separators=(",", ":")))
' 2>/dev/null || true)"
[ -n "$FINISH_FIELDS" ] || exit 0

PAYLOAD="$(RECORD="$FINISH_FIELDS" python3 -c '
import json, os
r = json.loads(os.environ["RECORD"])
text = ("Agent run finished | founder={founder} | tool={tool} | ticket={ticket} | "
        "finished_at={finished_at} | elapsed_seconds={elapsed_seconds} | outcome={outcome}").format(**r)
print(json.dumps({"text": text}, separators=(",", ":")))
' 2>/dev/null || true)"
[ -n "$PAYLOAD" ] || exit 0
post_json "$PAYLOAD" || exit 0

printf '%s\n' "$FINISH_FIELDS" >>"$STATE_DIR/completed-runs.jsonl" 2>/dev/null || exit 0
chmod 600 "$STATE_DIR/completed-runs.jsonl" 2>/dev/null || true
rm -f "$STATE_FILE" 2>/dev/null || true
exit 0
