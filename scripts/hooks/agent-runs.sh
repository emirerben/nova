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
elif raw_outcome in {"completed", "complete", "success", "succeeded", "ok"}:
    outcome = "completed"
else:
    outcome = "stopped"

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

write_state() {
  local content="$1" tmp
  tmp="$(mktemp "$STATE_DIR/.run.XXXXXX" 2>/dev/null || true)"
  [ -n "$tmp" ] || return 1
  umask 077
  printf '%s' "$content" >"$tmp" 2>/dev/null || { rm -f "$tmp"; return 1; }
  chmod 600 "$tmp" 2>/dev/null || true
  mv "$tmp" "$STATE_FILE" 2>/dev/null
}

read_state() {
  python3 -c '
import json, sys
try:
    state = json.load(open(sys.argv[1]))
    if isinstance(state, dict):
        print(json.dumps(state, separators=(",", ":")))
except (OSError, ValueError, TypeError):
    pass
' "$STATE_FILE" 2>/dev/null || true
}

LOCK_DIR="$STATE_DIR/.session-$state_key.lock"
mkdir "$LOCK_DIR" 2>/dev/null || exit 0
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

if [ "$ACTION" = "start" ]; then
  NOW_EPOCH="$(date +%s)"
  NOW_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  STATE="$(read_state)"
  if [ -z "$STATE" ]; then
    if [ -z "$TICKET" ]; then
      TOOL="$TOOL" SESSION_ID="$SESSION_ID" python3 -c '
import json, os
print(json.dumps({"tool": os.environ["TOOL"], "session_id": os.environ["SESSION_ID"], "tracked": False}, separators=(",", ":")))
' | { read -r ignored_state; write_state "$ignored_state"; }
      exit 0
    fi
    FOUNDER="${NOVA_AGENT_RUNS_FOUNDER:-$(git config user.name 2>/dev/null || true)}"
    [ -n "$FOUNDER" ] || FOUNDER="unknown"
    STATE="$(FOUNDER="$FOUNDER" TOOL="$TOOL" TICKET="$TICKET" SESSION_ID="$SESSION_ID" NOW_AT="$NOW_AT" NOW_EPOCH="$NOW_EPOCH" python3 -c '
import json, os
print(json.dumps({
    "founder": os.environ["FOUNDER"], "tool": os.environ["TOOL"], "ticket": os.environ["TICKET"],
    "session_id": os.environ["SESSION_ID"], "started_at": os.environ["NOW_AT"],
    "active_seconds": 0, "wait_seconds": 0, "last_started_epoch": int(os.environ["NOW_EPOCH"]),
    "last_stopped_epoch": None, "tracked": True,
}, separators=(",", ":")))
' 2>/dev/null || true)"
    [ -n "$STATE" ] || exit 0
    write_state "$STATE" || exit 0
    PAYLOAD="$(STATE="$STATE" python3 -c '
import json, os
s = json.loads(os.environ["STATE"])
print(json.dumps({"text": "Agent task started | founder={founder} | tool={tool} | ticket={ticket} | started_at={started_at}".format(**s)}, separators=(",", ":")))
' 2>/dev/null || true)"
    [ -n "$PAYLOAD" ] && post_json "$PAYLOAD" || true
    exit 0
  fi

  TRACKED="$(STATE="$STATE" python3 -c 'import json,os; print("yes" if json.loads(os.environ["STATE"]).get("tracked") else "no")' 2>/dev/null || true)"
  [ "$TRACKED" = "yes" ] || exit 0
  CURRENT_TICKET="$(STATE="$STATE" python3 -c 'import json,os; print(json.loads(os.environ["STATE"]).get("ticket", ""))' 2>/dev/null || true)"
  # A logical task stays anchored to its initial KRI key for this session.
  # Starting a different ticket requires a new Codex/Claude session.
  [ -n "$TICKET" ] && [ "$TICKET" != "$CURRENT_TICKET" ] && exit 0

  RESUMED="$(STATE="$STATE" NOW_EPOCH="$NOW_EPOCH" python3 -c '
import json, os
s = json.loads(os.environ["STATE"])
last = s.get("last_stopped_epoch")
wait = max(0, int(os.environ["NOW_EPOCH"]) - int(last)) if last is not None else 0
s["wait_seconds"] = int(s.get("wait_seconds", 0)) + wait
s["pending_wait_seconds"] = wait
s["last_started_epoch"] = int(os.environ["NOW_EPOCH"])
s["last_stopped_epoch"] = None
print(json.dumps({"state": s, "wait": wait}, separators=(",", ":")))
' 2>/dev/null || true)"
  [ -n "$RESUMED" ] || exit 0
  STATE="$(RESULT="$RESUMED" python3 -c 'import json,os; print(json.dumps(json.loads(os.environ["RESULT"])["state"], separators=(",", ":")))')"
  WAIT_INCREMENT="$(RESULT="$RESUMED" python3 -c 'import json,os; print(json.loads(os.environ["RESULT"])["wait"])')"
  write_state "$STATE" || exit 0
  PAYLOAD="$(STATE="$STATE" WAIT_INCREMENT="$WAIT_INCREMENT" NOW_AT="$NOW_AT" python3 -c '
import json, os
s = json.loads(os.environ["STATE"])
text = ("Agent task resumed | founder={founder} | tool={tool} | ticket={ticket} | resumed_at={now} | "
        "wait_seconds={wait} | total_wait_seconds={total}").format(
    founder=s["founder"], tool=s["tool"], ticket=s["ticket"], now=os.environ["NOW_AT"],
    wait=os.environ["WAIT_INCREMENT"], total=s["wait_seconds"])
print(json.dumps({"text": text}, separators=(",", ":")))
' 2>/dev/null || true)"
  [ -n "$PAYLOAD" ] && post_json "$PAYLOAD" || true
  exit 0
fi

STATE="$(read_state)"
[ -n "$STATE" ] || exit 0
TRACKED="$(STATE="$STATE" python3 -c 'import json,os; print("yes" if json.loads(os.environ["STATE"]).get("tracked") else "no")' 2>/dev/null || true)"
[ "$TRACKED" = "yes" ] || exit 0

NOW_EPOCH="$(date +%s)"
STOPPED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
CHECKPOINT="$(STATE="$STATE" NOW_EPOCH="$NOW_EPOCH" STOPPED_AT="$STOPPED_AT" OUTCOME="$OUTCOME" python3 -c '
import json, os
s = json.loads(os.environ["STATE"])
started = s.get("last_started_epoch")
if started is None:
    raise SystemExit(0)
active = max(0, int(os.environ["NOW_EPOCH"]) - int(started))
s["active_seconds"] = int(s.get("active_seconds", 0)) + active
s["last_started_epoch"] = None
s["last_stopped_epoch"] = int(os.environ["NOW_EPOCH"])
wait = int(s.get("pending_wait_seconds", 0))
s["pending_wait_seconds"] = 0
record = {
    "founder": s["founder"], "tool": s["tool"], "ticket": s["ticket"], "started_at": s["started_at"],
    "stopped_at": os.environ["STOPPED_AT"], "active_seconds": active,
    "wait_seconds": wait, "total_active_seconds": s["active_seconds"],
    "total_wait_seconds": s.get("wait_seconds", 0), "outcome": os.environ["OUTCOME"],
}
print(json.dumps({"state": s, "record": record}, separators=(",", ":")))
' 2>/dev/null || true)"
[ -n "$CHECKPOINT" ] || exit 0
STATE="$(RESULT="$CHECKPOINT" python3 -c 'import json,os; print(json.dumps(json.loads(os.environ["RESULT"])["state"], separators=(",", ":")))')"
RECORD="$(RESULT="$CHECKPOINT" python3 -c 'import json,os; print(json.dumps(json.loads(os.environ["RESULT"])["record"], separators=(",", ":")))')"
write_state "$STATE" || exit 0
PAYLOAD="$(RECORD="$RECORD" python3 -c '
import json, os
r = json.loads(os.environ["RECORD"])
text = ("Agent task stopped — waiting for next prompt | founder={founder} | tool={tool} | ticket={ticket} | stopped_at={stopped_at} | "
        "active_seconds={active_seconds} | total_active_seconds={total_active_seconds} | "
        "total_wait_seconds={total_wait_seconds} | outcome={outcome}").format(**r)
print(json.dumps({"text": text}, separators=(",", ":")))
' 2>/dev/null || true)"
[ -n "$PAYLOAD" ] || exit 0
post_json "$PAYLOAD" || exit 0
printf '%s\n' "$RECORD" >>"$STATE_DIR/completed-runs.jsonl" 2>/dev/null || exit 0
chmod 600 "$STATE_DIR/completed-runs.jsonl" 2>/dev/null || true
exit 0
