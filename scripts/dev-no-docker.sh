#!/usr/bin/env bash
# Nova — dev launcher that uses Homebrew postgres + redis instead of docker.
# Mirrors scripts/dev-auto.sh but skips the docker-compose infra step. Use this
# when:
#   - postgres@16 is already running via brew services
#   - redis is already running via brew services
#
# Companion: scripts/dev-stop.sh

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEV_DIR="$REPO/.dev"
PID_FILE="$DEV_DIR/pids"
mkdir -p "$DEV_DIR"

log() { printf '[dev-no-docker] %s\n' "$*"; }

# ── Stop prior run ───────────────────────────────────────────────────────────
if [[ -f "$PID_FILE" ]]; then
  log "Stopping previous dev processes..."
  while read -r pid; do
    [[ -n "$pid" ]] && kill "$pid" 2>/dev/null || true
  done < "$PID_FILE"
  rm -f "$PID_FILE"
  sleep 1
fi
for port in 3000 8000; do
  pids=$(lsof -ti ":$port" 2>/dev/null || true)
  [[ -n "$pids" ]] && kill -9 $pids 2>/dev/null || true
done

# ── Verify prerequisites ─────────────────────────────────────────────────────
[[ -f "$REPO/.env" ]] || { log "ERROR: $REPO/.env missing"; exit 1; }
[[ -d "$REPO/src/apps/api/.venv" ]] || { log "ERROR: api venv missing"; exit 1; }
[[ -d "$REPO/src/apps/web/node_modules" ]] || { log "ERROR: web node_modules missing"; exit 1; }

if ! lsof -nP -iTCP:5432 -sTCP:LISTEN >/dev/null 2>&1; then
  log "ERROR: postgres not listening on :5432 (start with 'brew services start postgresql@16')"
  exit 1
fi
if ! lsof -nP -iTCP:6379 -sTCP:LISTEN >/dev/null 2>&1; then
  log "ERROR: redis not listening on :6379 (start with 'brew services start redis')"
  exit 1
fi

# ── Load env ─────────────────────────────────────────────────────────────────
set -a
# shellcheck source=/dev/null
source "$REPO/.env"
set +a

# ── Migrations ───────────────────────────────────────────────────────────────
log "Running alembic migrations..."
(cd "$REPO/src/apps/api" && .venv/bin/alembic upgrade head) > "$DEV_DIR/migrate.log" 2>&1 || {
  log "Migrations failed — see $DEV_DIR/migrate.log"
  exit 1
}

# ── API ──────────────────────────────────────────────────────────────────────
log "Starting API on :8000 (uvicorn --reload)..."
(
  cd "$REPO/src/apps/api"
  exec .venv/bin/uvicorn app.main:app --reload --port 8000 --host 0.0.0.0
) > "$DEV_DIR/api.log" 2>&1 &
echo $! >> "$PID_FILE"

# ── Worker ───────────────────────────────────────────────────────────────────
log "Starting Celery worker (watchfiles auto-restart)..."
(
  cd "$REPO/src/apps/api"
  PATH="$REPO/src/apps/api/.venv/bin:$PATH" exec .venv/bin/watchfiles --filter python \
    'celery -A app.worker:celery_app worker --loglevel=info --concurrency=2' \
    app
) > "$DEV_DIR/worker.log" 2>&1 &
echo $! >> "$PID_FILE"

# ── Web ──────────────────────────────────────────────────────────────────────
log "Starting Next.js on :3000..."
(
  cd "$REPO/src/apps/web"
  exec npm run dev
) > "$DEV_DIR/web.log" 2>&1 &
echo $! >> "$PID_FILE"

sleep 2
log ""
log "Dev environment started:"
log "  API:      http://localhost:8000"
log "  Worker:   celery (auto-restart on .py edits)"
log "  Frontend: http://localhost:3000"
log ""
log "Logs:"
log "  tail -f $DEV_DIR/api.log"
log "  tail -f $DEV_DIR/worker.log"
log "  tail -f $DEV_DIR/web.log"
log ""
log "Stop everything: ./scripts/dev-stop.sh"
