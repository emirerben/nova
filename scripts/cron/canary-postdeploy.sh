#!/usr/bin/env bash
# Post-deploy canary tick, driven by launchd every 30 min (com.nova.canary).
#
# Deterministic (no LLM): checks prod health on every tick, and when it sees a
# NEW Fly release since the last tick it runs the post-deploy checks that were
# previously "eyeball prod within 48h". Alerts via macOS notification + log on
# any failure; quiet on success.
#
# Install:  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.nova.canary.plist
#           (render the plist from infra/launchd/com.nova.canary.plist — replace
#            __HOME__ with $HOME; see infra/launchd/README pattern in the plist)
# Disable:  launchctl bootout gui/$(id -u)/com.nova.canary
# Test now: bash scripts/cron/canary-postdeploy.sh

set -uo pipefail

export HOME="${HOME:-/Users/emirerben}"
export PATH="$HOME/.fly/bin:$HOME/.local/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

APP="nova-video"
HEALTH_URL="https://nova-video.fly.dev/health"
WEB_URL="https://nova-video.vercel.app"
STATE_FILE="$HOME/.nova/canary-last-release"
LOG_DIR="$HOME/.nova/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/canary-$(date +%Y%m%d).log"
exec >>"$LOG" 2>&1

alert() {
  echo "CANARY FAIL $(date '+%H:%M:%S'): $*"
  osascript -e "display notification \"$*\" with title \"Nova canary\" sound name \"Basso\"" 2>/dev/null || true
}

echo "--- canary tick $(date) ---"

# ── 1. Prod health (every tick) ───────────────────────────────────────────────
if ! curl -fsS --max-time 15 "$HEALTH_URL" >/dev/null; then
  alert "API health check failed: $HEALTH_URL"
  exit 1
fi

# ── 2. Web reachable (every tick) ─────────────────────────────────────────────
if ! curl -fsS --max-time 15 -o /dev/null "$WEB_URL"; then
  alert "web frontend unreachable: $WEB_URL"
  exit 1
fi

# ── 3. New-release detection + machine states (needs fly CLI) ────────────────
if command -v fly >/dev/null 2>&1; then
  CUR="$(fly releases --app "$APP" --json 2>/dev/null \
        | python3 -c 'import json,sys; rs=json.load(sys.stdin); print(rs[0].get("version","") if rs else "")' 2>/dev/null || true)"
  LAST="$(cat "$STATE_FILE" 2>/dev/null || true)"
  if [ -n "$CUR" ] && [ "$CUR" != "$LAST" ]; then
    echo "new release v$CUR (was ${LAST:-none}) — running post-deploy checks"
    sleep 20  # let machines settle
    BAD="$(fly machines list --app "$APP" --json 2>/dev/null \
          | python3 -c 'import json,sys; ms=json.load(sys.stdin); print(",".join(m["name"] for m in ms if m.get("state") not in ("started","stopped")))' 2>/dev/null || true)"
    if [ -n "$BAD" ]; then
      alert "release v$CUR: machines unhealthy: $BAD"
      exit 1
    fi
    if ! curl -fsS --max-time 15 "$HEALTH_URL" >/dev/null; then
      alert "release v$CUR: health check failed after deploy"
      exit 1
    fi
    echo "$CUR" > "$STATE_FILE"
    echo "release v$CUR healthy (api + machines + web)"
  fi
else
  echo "fly CLI not on PATH — release detection skipped (health checks still ran)"
fi

echo "canary OK"
