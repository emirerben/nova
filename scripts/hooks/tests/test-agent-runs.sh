#!/usr/bin/env bash
# Focused integration tests for the privacy-minimal agent lifecycle reporter.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/hooks/agent-runs.sh"
RECAP_SCRIPT="$REPO_ROOT/scripts/hooks/agent-runs-recap.sh"
INSTALL_SCRIPT="$REPO_ROOT/scripts/hooks/install-agent-runs.sh"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

STATE_DIR="$TMP_DIR/state"
ENV_FILE="$TMP_DIR/agent-runs.env"
PAYLOADS="$TMP_DIR/payloads.jsonl"
FAKE_BIN="$TMP_DIR/bin"
mkdir -p "$FAKE_BIN"

printf '%s\n' 'NOVA_AGENT_RUNS_WEBHOOK_URL=https://hooks.slack.test/services/T000/B000/secret' >"$ENV_FILE"
chmod 600 "$ENV_FILE"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'if [ "${FAKE_CURL_EXIT:-0}" -ne 0 ]; then exit "$FAKE_CURL_EXIT"; fi' \
  'for arg in "$@"; do' \
  '  case "$arg" in' \
  '    @-) cat >>"$FAKE_CURL_PAYLOADS"; printf "\\n" >>"$FAKE_CURL_PAYLOADS"; exit 0 ;;' \
  '    @*) cat "${arg#@}" >>"$FAKE_CURL_PAYLOADS"; printf "\\n" >>"$FAKE_CURL_PAYLOADS"; exit 0 ;;' \
  '  esac' \
  'done' \
  'cat >>"$FAKE_CURL_PAYLOADS"; printf "\\n" >>"$FAKE_CURL_PAYLOADS"' >"$FAKE_BIN/curl"
chmod +x "$FAKE_BIN/curl"

run_hook() {
  local action="$1" tool="$2" input="$3"
  printf '%s' "$input" | env \
    PATH="$FAKE_BIN:$PATH" \
    FAKE_CURL_PAYLOADS="$PAYLOADS" \
    NOVA_AGENT_RUNS_STATE_DIR="$STATE_DIR" \
    NOVA_AGENT_RUNS_ENV_FILE="$ENV_FILE" \
    NOVA_AGENT_RUNS_FOUNDER='Emir Test' \
    bash "$SCRIPT" "$action" "$tool"
}

assert_payload_count() {
  local expected="$1"
  local actual=0
  [ -f "$PAYLOADS" ] && actual="$(python3 - "$PAYLOADS" <<'PY'
import sys
print(sum(1 for line in open(sys.argv[1]) if line.strip()))
PY
)"
  [ "$actual" = "$expected" ] || { echo "expected $expected Slack payloads, got $actual" >&2; exit 1; }
}

# Codex start creates one minimal state entry and sends metadata only.
run_hook start codex '{"session_id":"codex-session-1","prompt":"Implement KRI-49 without retaining this prompt"}'
assert_payload_count 1
python3 - "$PAYLOADS" <<'PY'
import json, sys
payload = json.loads(open(sys.argv[1]).readline())
text = payload["text"]
assert "founder=Emir Test" in text and "tool=codex" in text and "ticket=KRI-49" in text
assert "without retaining" not in text and "session" not in text
assert set(payload) == {"text"}
PY

# A second prompt in the same session never starts another run.
run_hook start codex '{"session_id":"codex-session-1","prompt":"KRI-99 must not replace the initial ticket"}'
assert_payload_count 1

sleep 1
run_hook finish codex '{"session_id":"codex-session-1","status":"failed"}'
assert_payload_count 2
python3 - "$PAYLOADS" "$STATE_DIR/completed-runs.jsonl" <<'PY'
import json, sys
finish = json.loads(open(sys.argv[1]).read().splitlines()[1])
assert "outcome=failed" in finish["text"]
record = json.loads(open(sys.argv[2]).readline())
assert set(record) == {"founder", "tool", "ticket", "started_at", "stopped_at", "active_seconds", "wait_seconds", "total_active_seconds", "total_wait_seconds", "outcome"}
assert record["tool"] == "codex" and record["ticket"] == "KRI-49" and record["outcome"] == "failed"
assert record["active_seconds"] >= 1 and record["wait_seconds"] == 0
PY

# A resumed prompt retains the initial ticket, measures the human wait, and
# produces a separate stop checkpoint without double-counting cumulative time.
sleep 1
run_hook start codex '{"session_id":"codex-session-1","prompt":"Please continue"}'
assert_payload_count 3
run_hook finish codex '{"session_id":"codex-session-1"}'
assert_payload_count 4
python3 - "$STATE_DIR/completed-runs.jsonl" <<'PY'
import json, sys
records = [json.loads(line) for line in open(sys.argv[1])]
assert len(records) == 2
assert records[-1]["ticket"] == "KRI-49" and records[-1]["wait_seconds"] >= 1
assert records[-1]["total_wait_seconds"] >= records[-1]["wait_seconds"]
PY

# A ticket must be present in the initial prompt. A later ticket cannot opt in.
run_hook start claude '{"session_id":"claude-session-no-ticket","prompt":"Start without a ticket"}'
assert_payload_count 4
run_hook start claude '{"session_id":"claude-session-no-ticket","prompt":"Now KRI-49 should remain ignored"}'
assert_payload_count 4
run_hook finish claude '{"session_id":"claude-session-no-ticket"}'
assert_payload_count 4

# Claude uses the same lifecycle reporter and preserves its tool identity.
run_hook start claude '{"session_id":"claude-session-1","prompt":"KRI-49 test Claude lifecycle"}'
run_hook finish claude '{"session_id":"claude-session-1","outcome":"completed"}'
assert_payload_count 6
python3 - "$STATE_DIR/completed-runs.jsonl" <<'PY'
import json, sys
records = [json.loads(line) for line in open(sys.argv[1])]
assert [record["tool"] for record in records] == ["codex", "codex", "claude"]
PY

# A missing local secret config and a failed webhook are both fail-open. Failed
# finishes are intentionally absent from the recap ledger.
MISSING_ENV="$TMP_DIR/missing.env"
printf '%s' '{"session_id":"missing-config","prompt":"KRI-49 no config"}' | env \
  NOVA_AGENT_RUNS_STATE_DIR="$TMP_DIR/missing-state" \
  NOVA_AGENT_RUNS_ENV_FILE="$MISSING_ENV" \
  bash "$SCRIPT" start codex
printf '%s\n' 'NOVA_AGENT_RUNS_WEBHOOK_URL=https://hooks.slack.test/services/T000/B000/secret' >"$TMP_DIR/failing.env"
printf '%s' '{"session_id":"failing-finish","prompt":"KRI-49 webhook failure"}' | env \
  PATH="$FAKE_BIN:$PATH" FAKE_CURL_PAYLOADS="$PAYLOADS" FAKE_CURL_EXIT=1 \
  NOVA_AGENT_RUNS_STATE_DIR="$TMP_DIR/failing-state" NOVA_AGENT_RUNS_ENV_FILE="$TMP_DIR/failing.env" \
  NOVA_AGENT_RUNS_FOUNDER='Emir Test' bash "$SCRIPT" start codex
printf '%s' '{"session_id":"failing-finish"}' | env \
  PATH="$FAKE_BIN:$PATH" FAKE_CURL_PAYLOADS="$PAYLOADS" FAKE_CURL_EXIT=1 \
  NOVA_AGENT_RUNS_STATE_DIR="$TMP_DIR/failing-state" NOVA_AGENT_RUNS_ENV_FILE="$TMP_DIR/failing.env" \
  NOVA_AGENT_RUNS_FOUNDER='Emir Test' bash "$SCRIPT" finish codex
[ ! -f "$TMP_DIR/failing-state/completed-runs.jsonl" ] || { echo 'failed finish entered ledger' >&2; exit 1; }

# The recap totals active and human-wait time from locally reported stops once
# per local date. The marker
# prevents launchd retries from duplicating the Slack post.
RECAP_DATE="$(date +%F)"
env PATH="$FAKE_BIN:$PATH" FAKE_CURL_PAYLOADS="$PAYLOADS" \
  NOVA_AGENT_RUNS_STATE_DIR="$STATE_DIR" NOVA_AGENT_RUNS_ENV_FILE="$ENV_FILE" \
  NOVA_AGENT_RUNS_RECAP_DATE="$RECAP_DATE" bash "$RECAP_SCRIPT" >/dev/null
assert_payload_count 7
python3 - "$PAYLOADS" "$RECAP_DATE" <<'PY'
import json, sys
recap = json.loads([line for line in open(sys.argv[1]).read().splitlines() if line][-1])["text"]
assert sys.argv[2] in recap and "3 reported stop(s)" in recap and "waiting" in recap
assert "KRI-49" in recap and "codex" in recap and "claude" in recap
PY
env PATH="$FAKE_BIN:$PATH" FAKE_CURL_PAYLOADS="$PAYLOADS" \
  NOVA_AGENT_RUNS_STATE_DIR="$STATE_DIR" NOVA_AGENT_RUNS_ENV_FILE="$ENV_FILE" \
  NOVA_AGENT_RUNS_RECAP_DATE="$RECAP_DATE" bash "$RECAP_SCRIPT" >/dev/null
assert_payload_count 7

# Installation creates user-only local state/config and renders, but never
# activates, the schedule. A temporary HOME proves it cannot touch real config.
INSTALL_HOME="$TMP_DIR/install-home"
env HOME="$INSTALL_HOME" NOVA_AGENT_RUNS_STATE_DIR="$INSTALL_HOME/.nova/agent-runs" \
  NOVA_AGENT_RUNS_ENV_FILE="$INSTALL_HOME/.nova/agent-runs.env" bash "$INSTALL_SCRIPT" >/dev/null
[ "$(stat -f '%Lp' "$INSTALL_HOME/.nova/agent-runs")" = 700 ] || { echo 'state directory mode is not 700' >&2; exit 1; }
[ "$(stat -f '%Lp' "$INSTALL_HOME/.nova/agent-runs.env")" = 600 ] || { echo 'config file mode is not 600' >&2; exit 1; }
INSTALL_PLIST="$INSTALL_HOME/Library/LaunchAgents/com.nova.agent-runs-recap.plist"
plutil -lint "$INSTALL_PLIST" >/dev/null
rg -q '<integer>2</integer>' "$INSTALL_PLIST" && rg -q '<integer>6</integer>' "$INSTALL_PLIST" || {
  echo 'rendered plist is not scheduled for Monday through Friday' >&2; exit 1;
}

echo 'agent-runs hook tests passed'
