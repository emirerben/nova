#!/usr/bin/env bash
# Nova — stop the dev environment started by dev-auto.sh

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEV_DIR="$REPO/.dev"
PID_FILE="$DEV_DIR/pids"

log() { printf '[dev-stop] %s\n' "$*"; }

if [[ -f "$PID_FILE" ]]; then
  log "Stopping tracked processes..."
  while read -r pid; do
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      # Kill the whole process group so uvicorn reloader + celery workers die cleanly
      kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    fi
  done < "$PID_FILE"
  rm -f "$PID_FILE"
fi

# Free dev ports just in case anything orphaned
for port in 3000 8000; do
  pids=$(lsof -ti ":$port" 2>/dev/null || true)
  if [[ -n "$pids" ]]; then
    log "Freeing port $port..."
    kill -9 $pids 2>/dev/null || true
  fi
done

log "Stopping redis + postgres containers..."
(cd "$REPO" && docker-compose stop redis db) > /dev/null 2>&1 || true

log "Done."
