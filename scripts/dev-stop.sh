#!/usr/bin/env bash
# Kria — stop the dev environment started by dev-auto.sh

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

# Ports and infra are shared by worktrees. Do not kill an untracked process on
# :3000/:8000 or stop the shared Redis/Postgres containers; only the PIDs in
# this checkout's .dev/pids file are in scope above. This makes stopping one
# worktree unable to disrupt another worktree's worker or API.
log "Leaving shared dev ports and Redis/Postgres untouched."

log "Done."
