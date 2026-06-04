#!/usr/bin/env bash
# queue.sh — ergonomic wrapper over scripts/admin.py for the dev-loop build queue.
# Saves you writing raw admin.py JSON for the common operations.
#
#   queue.sh add "title" ["body"] [priority]   mint one task (default priority 100)
#   queue.sh ls [status]                       list tasks (queued|in_progress|gating|
#                                              awaiting_approval|blocked|done)
#   queue.sh block <id>                        force a task to `blocked`
#   queue.sh reset <id>                        un-block / re-queue a task
#   queue.sh sync [TASKS.md]                   mint a task per new `- [ ]` item
#                                              (idempotent: skips titles already queued)
#
# Talks to PROD by default (the dev-loop queue lives on Fly). Resolves
# ADMIN_PROD_API_KEY from the env or ~/.nova/dev-loop.env, and points TLS at
# certifi (macOS python.org Python ships no CA bundle) — same self-heal as the
# dev-loop lib, so this works on the home box out of the box.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || { echo "ERROR: repo root not found" >&2; exit 1; }

# Pull the prod admin key from the dev-loop secrets file if it isn't already set.
ENV_FILE="${NOVA_DEV_LOOP_ENV:-$HOME/.nova/dev-loop.env}"
if [ -z "${ADMIN_PROD_API_KEY:-}" ] && [ -f "$ENV_FILE" ]; then
  ADMIN_PROD_API_KEY="$(grep -E '^[[:space:]]*ADMIN_PROD_API_KEY=' "$ENV_FILE" | head -1 | cut -d= -f2-)"
  export ADMIN_PROD_API_KEY
fi
if [ -z "${SSL_CERT_FILE:-}" ]; then
  _certifi="$(python3 -c 'import certifi; print(certifi.where())' 2>/dev/null || true)"
  [ -n "$_certifi" ] && export SSL_CERT_FILE="$_certifi"
fi

ADMIN=(python3 scripts/admin.py --prod --yes)

_json_str() { python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$1"; }

cmd_add() {
  local title="${1:-}" body="${2:-}" priority="${3:-100}"
  [ -z "$title" ] && { echo "usage: queue.sh add \"title\" [\"body\"] [priority]" >&2; exit 2; }
  "${ADMIN[@]}" POST build-tasks --json "$(python3 - "$title" "$body" "$priority" <<'PY'
import json, sys
title, body, priority = sys.argv[1], sys.argv[2], int(sys.argv[3])
p = {"title": title, "priority": priority}
if body:
    p["body"] = body
print(json.dumps(p))
PY
)"
}

cmd_ls() {
  local status="${1:-}"
  local path="build-tasks"
  [ -n "$status" ] && path="build-tasks?status=$status"
  "${ADMIN[@]}" GET "$path" | python3 -c '
import json, sys
d = json.load(sys.stdin)
items = d.get("items", [])
if not items:
    print("(no tasks)"); sys.exit(0)
for t in items:
    pr = t.get("pr_url") or ""
    line = "%s  %-17s  p%-4s %s" % (t["id"][:8], t["status"], t.get("priority", 100), t["title"][:60])
    if pr:
        line += "  " + pr
    print(line)
'
}

cmd_patch() {
  local action="$1" id="${2:-}"
  [ -z "$id" ] && { echo "usage: queue.sh $action <id>" >&2; exit 2; }
  "${ADMIN[@]}" PATCH "build-tasks/$id" --json "{\"action\": \"$action\"}"
}

cmd_sync() {
  local file="${1:-$REPO_ROOT/TASKS.md}"
  [ -f "$file" ] || { echo "ERROR: $file not found" >&2; exit 1; }

  # Existing titles (any status) — the dedup set, so re-running never re-mints.
  local existing
  existing="$("${ADMIN[@]}" GET build-tasks | python3 -c 'import json,sys; print("\n".join(t["title"] for t in json.load(sys.stdin).get("items", [])))')" || {
    echo "ERROR: could not list existing tasks (API unreachable / key unset?)" >&2; exit 1; }

  local minted=0 skipped=0
  while IFS= read -r task_json; do
    [ -z "$task_json" ] && continue
    local title body priority
    title="$(printf '%s' "$task_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["title"])')"
    if printf '%s\n' "$existing" | grep -qxF -- "$title"; then
      skipped=$((skipped + 1)); continue
    fi
    body="$(printf '%s' "$task_json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("body",""))')"
    priority="$(printf '%s' "$task_json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("priority",100))')"
    echo "[queue] minting: $title"
    "${ADMIN[@]}" POST build-tasks --json "$task_json" >/dev/null && minted=$((minted + 1))
  done < <(python3 scripts/queue_sync.py "$file")

  echo "[queue] sync done: minted $minted, skipped $skipped already-queued."
}

case "${1:-}" in
  add)   shift; cmd_add "$@" ;;
  ls)    shift; cmd_ls "$@" ;;
  block) shift; cmd_patch block "$@" ;;
  reset) shift; cmd_patch reset "$@" ;;
  sync)  shift; cmd_sync "$@" ;;
  *) echo "usage: queue.sh {add|ls|block|reset|sync} ..." >&2; exit 2 ;;
esac
