#!/usr/bin/env bash
# Nova — one-command dev environment with hot reload
#
# Starts:
#   - redis + postgres via docker-compose (infra only)
#   - alembic migrations
#   - API   → uvicorn --reload  (hot reload on .py changes)
#   - worker→ watchfiles + celery (hot reload on .py changes)
#   - web   → next dev (Next.js HMR)
#
# All logs go to .dev/<service>.log. Processes run in the background.
# Safe to re-run — kills prior dev processes first.
#
# Companion: scripts/dev-stop.sh

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEV_DIR="$REPO/.dev"
PID_FILE="$DEV_DIR/pids"
mkdir -p "$DEV_DIR"

log() { printf '[dev-auto] %s\n' "$*"; }

# ── 1. Stop prior run ─────────────────────────────────────────────────────────
if [[ -f "$PID_FILE" ]]; then
  log "Stopping previous dev processes..."
  while read -r pid; do
    [[ -n "$pid" ]] && kill "$pid" 2>/dev/null || true
  done < "$PID_FILE"
  rm -f "$PID_FILE"
  sleep 1
fi

# Also free our dev ports (8000 api, 3000 web) in case of orphan processes
for port in 3000 8000; do
  pids=$(lsof -ti ":$port" 2>/dev/null || true)
  [[ -n "$pids" ]] && kill -9 $pids 2>/dev/null || true
done

# ── 2. Verify prerequisites ───────────────────────────────────────────────────
if [[ ! -f "$REPO/.env" ]]; then
  log "ERROR: $REPO/.env not found. Run: cp .env.example .env"
  exit 1
fi

if [[ ! -d "$REPO/src/apps/api/.venv" ]]; then
  log "ERROR: Python venv not found at src/apps/api/.venv"
  log "Run: (cd src/apps/api && python3 -m venv .venv && .venv/bin/pip install -e '.[dev]')"
  exit 1
fi

if [[ ! -d "$REPO/src/apps/web/node_modules" ]]; then
  log "ERROR: web/node_modules not found. Run: (cd src/apps/web && npm install)"
  exit 1
fi

# ── 3. Start infra (redis + postgres only) ───────────────────────────────────
log "Starting redis + postgres via docker-compose..."
(cd "$REPO" && docker-compose up -d redis db) > "$DEV_DIR/infra.log" 2>&1

log "Waiting for postgres to be ready..."
for _ in {1..30}; do
  if (cd "$REPO" && docker-compose exec -T db pg_isready -U postgres) >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

# ── 4. Load env (force localhost networking) ─────────────────────────────────
set -a
# shellcheck source=/dev/null
source "$REPO/.env"
set +a
export REDIS_URL="${REDIS_URL:-redis://localhost:6379}"
export DATABASE_URL="${DATABASE_URL:-postgresql://postgres:postgres@localhost:5432/nova}"

# ── 5. Run migrations ─────────────────────────────────────────────────────────
log "Running alembic migrations..."
(cd "$REPO/src/apps/api" && .venv/bin/alembic upgrade head) > "$DEV_DIR/migrate.log" 2>&1 || {
  log "Migrations failed — see $DEV_DIR/migrate.log"
  exit 1
}

# ── 6. Start API (hot reload via uvicorn --reload) ───────────────────────────
log "Starting API on :8000 (uvicorn --reload)..."
(
  cd "$REPO/src/apps/api"
  exec .venv/bin/uvicorn app.main:app --reload --port 8000 --host 0.0.0.0
) > "$DEV_DIR/api.log" 2>&1 &
echo $! >> "$PID_FILE"

# ── 7. Start worker (hot reload via watchfiles) ──────────────────────────────
log "Starting Celery worker (watchfiles auto-restart on .py changes)..."
(
  cd "$REPO/src/apps/api"
  exec .venv/bin/watchfiles --filter python \
    'celery -A app.worker:celery_app worker --loglevel=info --concurrency=2' \
    app
) > "$DEV_DIR/worker.log" 2>&1 &
echo $! >> "$PID_FILE"

# ── 8. Start web (Next.js HMR) ───────────────────────────────────────────────
log "Starting Next.js on :3000..."
(
  cd "$REPO/src/apps/web"
  exec npm run dev
) > "$DEV_DIR/web.log" 2>&1 &
echo $! >> "$PID_FILE"

# ── 9. Summary ────────────────────────────────────────────────────────────────
sleep 2
log ""
log "Dev environment started:"
log "  API:      http://localhost:8000   (reload on .py edits)"
log "  Worker:   celery               (restart on .py edits via watchfiles)"
log "  Frontend: http://localhost:3000   (Next.js HMR)"
log ""
log "Logs:"
log "  tail -f $DEV_DIR/api.log"
log "  tail -f $DEV_DIR/worker.log"
log "  tail -f $DEV_DIR/web.log"
log ""
log "Stop everything:  ./scripts/dev-stop.sh"
