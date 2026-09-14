#!/usr/bin/env bash
# Send one privacy-safe daily Slack recap for locally reported agent-run finishes.
# Intended for launchd, but safe to invoke manually. A successful post writes a
# per-date marker so a repeat invocation cannot send a second recap.

set -uo pipefail

STATE_DIR="${NOVA_AGENT_RUNS_STATE_DIR:-$HOME/.nova/agent-runs}"
ENV_FILE="${NOVA_AGENT_RUNS_ENV_FILE:-$HOME/.nova/agent-runs.env}"
RECAP_DATE="${NOVA_AGENT_RUNS_RECAP_DATE:-$(date +%F)}"
LEDGER="$STATE_DIR/completed-runs.jsonl"
MARKER="$STATE_DIR/recap-${RECAP_DATE}.sent"
LOCK_DIR="$STATE_DIR/.recap-${RECAP_DATE}.lock"

log() { printf '%s [agent-runs-recap] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >&2; }

# The reporting hook creates this directory too; keep the installer/recap
# contract explicit when the recap is invoked independently.
mkdir -p "$STATE_DIR" 2>/dev/null || { log "cannot create state directory; skipping"; exit 0; }
chmod 700 "$STATE_DIR" 2>/dev/null || { log "cannot secure state directory; skipping"; exit 0; }

if [ -e "$MARKER" ]; then
  log "recap for $RECAP_DATE already sent"
  exit 0
fi

# Do not source a config file another local account can modify. A missing or
# unreadable config is intentionally fail-open: reporting must not break hooks.
if [ ! -f "$ENV_FILE" ] || [ ! -O "$ENV_FILE" ] || [ ! -r "$ENV_FILE" ]; then
  log "owner-readable config is unavailable; skipping"
  exit 0
fi
# shellcheck disable=SC1090 -- this is the user's local, owner-readable config.
. "$ENV_FILE" || { log "could not source config; skipping"; exit 0; }
if [ -z "${NOVA_AGENT_RUNS_WEBHOOK_URL:-}" ]; then
  log "NOVA_AGENT_RUNS_WEBHOOK_URL is unset; skipping"
  exit 0
fi
if [ ! -f "$LEDGER" ]; then
  log "no completed-run ledger yet; skipping"
  exit 0
fi

# mkdir is atomic. It prevents two manual/launchd invocations from both posting
# before either has written the marker. A stale lock is fail-open (no duplicate
# post); remove it manually only after confirming no recap process is active.
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  log "another recap invocation owns the date lock; skipping"
  exit 0
fi
cleanup() { rmdir "$LOCK_DIR" 2>/dev/null || true; }
trap cleanup EXIT HUP INT TERM

PAYLOAD_FILE="$(mktemp "$STATE_DIR/.recap-payload.XXXXXX")" || { log "cannot create payload; skipping"; exit 0; }
chmod 600 "$PAYLOAD_FILE" 2>/dev/null || true
trap 'rm -f "$PAYLOAD_FILE"; cleanup' EXIT HUP INT TERM

# Python is used only for JSON/date handling; it deliberately emits metadata
# rather than prompts, transcripts, command output, paths, or other run content.
python3 - "$LEDGER" "$RECAP_DATE" >"$PAYLOAD_FILE" <<'PY'
import json
import sys
from datetime import datetime

ledger, recap_date = sys.argv[1:]


def local_date(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone().date().isoformat() if parsed.tzinfo else parsed.date().isoformat()


runs = []
with open(ledger, encoding="utf-8") as source:
    for line in source:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        # Inclusion in this ledger means the finish was reported successfully.
        # `outcome` describes the run itself (completed, failed, interrupted).
        if not isinstance(item, dict):
            continue
        stopped_at = item.get("stopped_at") or item.get("finished_at")
        if local_date(stopped_at) != recap_date:
            continue
        required = ("founder", "tool", "ticket", "started_at", "outcome")
        if not all(key in item for key in required):
            continue
        item["stopped_at"] = stopped_at
        item["active_seconds"] = item.get("active_seconds", item.get("elapsed_seconds", 0))
        item["wait_seconds"] = item.get("wait_seconds", 0)
        runs.append(item)

if not runs:
    sys.exit(3)

def duration(value: object) -> str:
    try:
        seconds = max(0, int(float(value)))
    except (TypeError, ValueError):
        return "unknown duration"
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}h {minutes}m" if hours else (f"{minutes}m {seconds}s" if minutes else f"{seconds}s")

def seconds(value: object) -> int:
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0

active_total = sum(seconds(item["active_seconds"]) for item in runs)
wait_total = sum(seconds(item["wait_seconds"]) for item in runs)
lines = [
    f"*Agent runs recap — {recap_date}*",
    f"{len(runs)} reported stop(s) · active {duration(active_total)} · waiting {duration(wait_total)}.",
]
for item in runs:
    # Values are serialized by json.dumps below, so Slack receives text only.
    lines.append(
        f"• {item['founder']} · {item['tool']} · {item['ticket']} · "
        f"active {duration(item['active_seconds'])} · waiting {duration(item['wait_seconds'])} · {item['outcome']}"
    )

print(json.dumps({"text": "\n".join(lines)}, ensure_ascii=False))
PY
status=$?
if [ "$status" -ne 0 ]; then
  if [ "$status" -eq 3 ]; then
    log "no reported finishes for $RECAP_DATE; skipping"
  else
    log "could not build recap payload; skipping"
  fi
  exit 0
fi

if ! curl --fail --silent --show-error --max-time 20 \
  -H 'Content-Type: application/json' \
  --data-binary "@$PAYLOAD_FILE" \
  "$NOVA_AGENT_RUNS_WEBHOOK_URL" >/dev/null; then
  log "Slack post failed; marker was not written"
  exit 0
fi

MARKER_TMP="$(mktemp "$STATE_DIR/.recap-marker.XXXXXX")" || { log "post succeeded but marker creation failed"; exit 0; }
chmod 600 "$MARKER_TMP" 2>/dev/null || true
if ! printf 'sent_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$MARKER_TMP"; then
  rm -f "$MARKER_TMP"
  log "post succeeded but marker write failed"
  exit 0
fi
if ! mv "$MARKER_TMP" "$MARKER"; then
  rm -f "$MARKER_TMP"
  log "post succeeded but marker move failed"
  exit 0
fi
log "sent recap for $RECAP_DATE"
