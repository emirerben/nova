#!/usr/bin/env bash
# Kria — one-command dev environment with hot reload
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
api_port="${NOVA_DEV_API_PORT:-8000}"
web_port="${NOVA_DEV_WEB_PORT:-3000}"
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

# Also free this worktree's selected ports in case of orphan processes.
for port in "$web_port" "$api_port"; do
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

if ! ffmpeg -hide_banner -h filter=subtitles >/dev/null 2>&1; then
  log "ERROR: ffmpeg was built without libass subtitles filter support."
  log "Fix: brew install ffmpeg-full and prepend /opt/homebrew/opt/ffmpeg-full/bin to PATH"
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
# Explicit dev-only overrides let an isolated QA run use filesystem storage
# without editing the shared, symlinked repo-root .env (which would affect
# every worktree). Production never invokes dev-auto.sh.
export STORAGE_PROVIDER="${NOVA_DEV_STORAGE_PROVIDER:-$STORAGE_PROVIDER}"
export E2E_FIXTURES="${NOVA_DEV_E2E_FIXTURES:-${E2E_FIXTURES:-false}}"
local_storage_root="${NOVA_DEV_LOCAL_STORAGE_ROOT:-${LOCAL_STORAGE_ROOT:-}}"
if [[ -n "$local_storage_root" ]]; then
  export LOCAL_STORAGE_ROOT="$local_storage_root"
fi
local_storage_base_url="${NOVA_DEV_LOCAL_STORAGE_BASE_URL:-${LOCAL_STORAGE_BASE_URL:-}}"
if [[ -n "$local_storage_base_url" ]]; then
  export LOCAL_STORAGE_BASE_URL="$local_storage_base_url"
fi
export REDIS_URL="${NOVA_DEV_REDIS_URL:-${REDIS_URL:-redis://localhost:6379}}"
export DATABASE_URL="${NOVA_DEV_DATABASE_URL:-${DATABASE_URL:-postgresql://postgres:postgres@localhost:5432/nova}}"
export API_URL="${NOVA_DEV_API_URL:-http://localhost:$api_port}"
export NEXT_PUBLIC_API_URL="$API_URL"
export NEXTAUTH_URL="${NOVA_DEV_NEXTAUTH_URL:-http://localhost:$web_port}"
if [[ "$STORAGE_PROVIDER" == "local" && -z "${NOVA_DEV_LOCAL_STORAGE_BASE_URL:-}" ]]; then
  export LOCAL_STORAGE_BASE_URL="http://127.0.0.1:$api_port/dev-qa/storage"
fi

# ── 5. Run migrations ─────────────────────────────────────────────────────────
log "Running alembic migrations..."
(cd "$REPO/src/apps/api" && .venv/bin/alembic upgrade head) > "$DEV_DIR/migrate.log" 2>&1 || {
  log "Migrations failed — see $DEV_DIR/migrate.log"
  exit 1
}

# ── 6. Start API (hot reload via uvicorn --reload) ───────────────────────────
log "Starting API on :$api_port (uvicorn --reload)..."
(
  cd "$REPO/src/apps/api"
  exec .venv/bin/uvicorn app.main:app --reload --port "$api_port" --host 0.0.0.0
) > "$DEV_DIR/api.log" 2>&1 &
echo $! >> "$PID_FILE"

# ── 7. Start worker (hot reload via watchfiles) ──────────────────────────────
# -Q celery,plan-jobs,overlay-jobs,creator-guided-jobs: drain the default queue, the content-plan
# render queue (per-item + activation renders are routed to plan-jobs via
# enqueue_orchestrator_sync), AND the SFX/media-overlay edit queue (sound-effects
# + overlay renders route to overlay-jobs via dispatch_set_sound_effects /
# dispatch_set_media_overlays). Must match prod fly.toml — without these queues
# the corresponding renders sit unconsumed and produce no output locally.
log "Starting Celery worker (watchfiles auto-restart on .py changes)..."
# CELERY_POOL: prefork (default, matches prod) | threads. On some macOS setups
# (observed on Python 3.14 venvs) prefork children die in a SIGKILL boot loop
# ("Timed out waiting for UP message") — set CELERY_POOL=threads in .env.
(
  cd "$REPO/src/apps/api"
  exec .venv/bin/watchfiles --filter python \
    ".venv/bin/celery -A app.worker:celery_app worker --loglevel=info --concurrency=2 --pool=${CELERY_POOL:-prefork} -Q celery,plan-jobs,overlay-jobs,creator-guided-jobs" \
    app
) > "$DEV_DIR/worker.log" 2>&1 &
echo $! >> "$PID_FILE"

# ── 7b. Overlay worker (--pool=solo, no fork) ────────────────────────────────
# Media-overlay apply tasks load FFmpeg + GCS but NOT the CLIP model.  On macOS,
# forked prefork children inherit the CLIP singleton's C-library handles and
# crash (SIGSEGV) when they first touch those handles.  A solo worker avoids
# the fork entirely.  overlay-jobs is a lightweight, fast queue (< 60s per
# task) so solo concurrency is fine.
log "Starting overlay worker (--pool=solo for macOS fork-safety)..."
(
  cd "$REPO/src/apps/api"
  exec .venv/bin/celery -A app.worker:celery_app worker \
    --pool=solo --loglevel=info \
    -Q overlay-jobs \
    -n worker-overlay@%h
) >> "$DEV_DIR/worker.log" 2>&1 &
echo $! >> "$PID_FILE"

# ── 8. Start web (Next.js HMR) ───────────────────────────────────────────────
log "Starting Next.js on :$web_port..."
(
  cd "$REPO/src/apps/web"
  exec npm run dev -- --port "$web_port"
) > "$DEV_DIR/web.log" 2>&1 &
echo $! >> "$PID_FILE"

# ── 9. Summary ────────────────────────────────────────────────────────────────
sleep 2
log ""
log "Dev environment started:"
log "  API:      http://localhost:$api_port   (reload on .py edits)"
log "  Worker:   celery               (restart on .py edits via watchfiles)"
log "  Frontend: http://localhost:$web_port   (Next.js HMR)"
log ""
log "Logs:"
log "  tail -f $DEV_DIR/api.log"
log "  tail -f $DEV_DIR/worker.log"
log "  tail -f $DEV_DIR/web.log"
log ""
log "Stop everything:  ./scripts/dev-stop.sh"
