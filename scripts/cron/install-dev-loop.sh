#!/usr/bin/env bash
# install-dev-loop.sh — one-time, idempotent setup for the autonomous dev-loop on
# the home-box Mac. Safe to re-run.
#
#   1. clone the dedicated dev-loop checkout ($NOVA_DEV_LOOP_REPO) if missing —
#      the loop owns it; the builder's `git checkout -B` must never touch your
#      interactive repo.
#   2. scaffold the secrets file ($NOVA_DEV_LOOP_ENV, chmod 600) if missing, and
#      refuse if ADMIN_PROD_API_KEY ever lands in the checkout's .env.
#   3. render the launchd plist into ~/Library/LaunchAgents/ with absolute paths.
#
# Does NOT enable the timer (manual-trigger-first rollout) — it prints the
# launchctl command to run once a manual tick is proven end-to-end.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ORIGIN_URL="$(git -C "$REPO_ROOT" remote get-url origin 2>/dev/null || echo "https://github.com/emirerben/nova.git")"

DEV_LOOP_REPO="${NOVA_DEV_LOOP_REPO:-$HOME/.nova/loop/nova}"
ENV_FILE="${NOVA_DEV_LOOP_ENV:-$HOME/.nova/dev-loop.env}"
PLIST_SRC="$REPO_ROOT/infra/launchd/com.nova.dev-loop.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.nova.dev-loop.plist"
WRAPPER="$DEV_LOOP_REPO/scripts/cron/dev_loop_tick.sh"
LABEL="com.nova.dev-loop"

mkdir -p "$(dirname "$DEV_LOOP_REPO")" "$HOME/.nova/logs" "$HOME/Library/LaunchAgents"

# ── 1. dedicated checkout ────────────────────────────────────────────────────
if [ ! -d "$DEV_LOOP_REPO/.git" ]; then
  echo "[install] cloning $ORIGIN_URL -> $DEV_LOOP_REPO"
  git clone "$ORIGIN_URL" "$DEV_LOOP_REPO"
else
  echo "[install] checkout exists ($DEV_LOOP_REPO); fetching origin/main"
  git -C "$DEV_LOOP_REPO" fetch origin main --quiet || true
fi

# Seed a .env for the gate's tests, but NEVER carry the prod admin key there.
if [ ! -f "$DEV_LOOP_REPO/.env" ] && [ -f "$DEV_LOOP_REPO/.env.example" ]; then
  cp "$DEV_LOOP_REPO/.env.example" "$DEV_LOOP_REPO/.env"
  echo "[install] seeded $DEV_LOOP_REPO/.env from .env.example (keep prod keys OUT of it)"
fi
if [ -f "$DEV_LOOP_REPO/.env" ] && grep -qE '^[[:space:]]*ADMIN_PROD_API_KEY[[:space:]]*=' "$DEV_LOOP_REPO/.env"; then
  echo "[install] ERROR: ADMIN_PROD_API_KEY is in $DEV_LOOP_REPO/.env — remove it." >&2
  echo "  The headless builder runs bypassPermissions; the prod key must live ONLY in $ENV_FILE." >&2
  exit 1
fi

# ── 2. secrets file ──────────────────────────────────────────────────────────
if [ ! -f "$ENV_FILE" ]; then
  echo "[install] scaffolding $ENV_FILE (chmod 600 — fill in ADMIN_PROD_API_KEY)"
  ( umask 077; cat > "$ENV_FILE" <<'ENVEOF'
# Dev-loop secrets — sourced by scripts/cron/dev_loop_tick.sh. Keep at chmod 600.
# REQUIRED: the prod admin token (`fly secrets list -a nova-video` / your vault).
ADMIN_PROD_API_KEY=
# OPTIONAL: only if `gh` is not already logged in on this box.
# GH_TOKEN=
# OPTIONAL per-run caps:
# NOVA_BUILDER_TIMEOUT_S=900
# NOVA_GATE_TIMEOUT_S=2400
ENVEOF
  )
  chmod 600 "$ENV_FILE"
fi
if ! grep -qE '^[[:space:]]*ADMIN_PROD_API_KEY[[:space:]]*=[^[:space:]]' "$ENV_FILE"; then
  echo "[install] NOTE: $ENV_FILE has no ADMIN_PROD_API_KEY value yet — set it before the first tick."
fi

# ── 3. render plist ──────────────────────────────────────────────────────────
if [ ! -f "$PLIST_SRC" ]; then
  echo "[install] ERROR: plist template $PLIST_SRC missing" >&2; exit 1
fi
sed -e "s|__DEV_LOOP_TICK__|$WRAPPER|g" -e "s|__HOME__|$HOME|g" "$PLIST_SRC" > "$PLIST_DST"
echo "[install] rendered $PLIST_DST"

cat <<NEXT

[install] done. Manual-trigger-first rollout:
  1. set ADMIN_PROD_API_KEY in $ENV_FILE
  2. seed a task:
       (cd "$DEV_LOOP_REPO" && ADMIN_PROD_API_KEY=\$ADMIN_PROD_API_KEY \\
         python scripts/admin.py --prod POST build-tasks \\
         --json '{"title":"...","body":"..."}')
  3. prove a tick (bypasses the work-hours guard):
       NOVA_BUILDER_FORCE=1 bash "$WRAPPER" builder   # then: ... gate
  4. watch:    (cd "$DEV_LOOP_REPO" && python scripts/admin.py --prod GET build-tasks)
  5. ENABLE the recurring timer (only after 3 proves out):
       launchctl bootstrap gui/\$(id -u) "$PLIST_DST"
     disable it again:
       launchctl bootout gui/\$(id -u)/$LABEL
NEXT
