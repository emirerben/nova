#!/usr/bin/env bash
# worktree-setup.sh — idempotent bootstrap for a fresh worktree.
#
# Kills the ~30-min per-session bring-up: symlinks .env / .venv / node_modules
# from the primary checkout, checks for already-bound infra ports, and runs
# migrations if the DB is reachable. Safe to re-run at any time.
#
# Usage:  bash scripts/worktree-setup.sh          (from inside the worktree)
# Called automatically by scripts/new-session.sh for new worktrees.
#
# Override the primary checkout location with NOVA_PRIMARY if it ever moves.

set -uo pipefail

PRIMARY="${NOVA_PRIMARY:-$HOME/Projects/nova}"
REPO="$(git rev-parse --show-toplevel 2>/dev/null)" || {
  echo "ERROR: not inside a git repository." >&2
  exit 2
}

log()  { printf '[worktree-setup] %s\n' "$*"; }
warn() { printf '[worktree-setup] WARN: %s\n' "$*"; }

if [ "$REPO" = "$PRIMARY" ]; then
  log "This IS the primary checkout ($PRIMARY) — nothing to link. Exiting."
  exit 0
fi
if [ ! -d "$PRIMARY" ]; then
  warn "primary checkout not found at $PRIMARY (set NOVA_PRIMARY to override); skipping symlinks."
  exit 1
fi

# ── 1. Symlink shared, git-ignored assets from the primary checkout ──────────
# Each link is skipped if the target already exists (real dir/file or link).
link() {
  local src="$1" dst="$2"
  if [ -e "$dst" ] || [ -L "$dst" ]; then
    log "exists, skipping: ${dst#"$REPO"/}"
  elif [ -e "$src" ]; then
    ln -s "$src" "$dst"
    log "linked ${dst#"$REPO"/} -> $src"
  else
    warn "primary has no ${src#"$PRIMARY"/} — create it there first."
  fi
}

link "$PRIMARY/.env"                      "$REPO/.env"
link "$PRIMARY/.env.local-render"         "$REPO/.env.local-render"
link "$PRIMARY/src/apps/api/.venv"        "$REPO/src/apps/api/.venv"
link "$PRIMARY/src/apps/api/.venv-test"   "$REPO/src/apps/api/.venv-test"
link "$PRIMARY/src/apps/web/node_modules" "$REPO/src/apps/web/node_modules"
link "$PRIMARY/src/apps/web/.env.local"   "$REPO/src/apps/web/.env.local"

# The shared venv's editable install points at the PRIMARY checkout. Running
# pytest/uvicorn from this worktree's src/apps/api is safe (cwd shadows the
# editable path), but never invoke `python -c "import app"` from elsewhere.

# ── 2. Infra port check ───────────────────────────────────────────────────────
for spec in "5432:postgres" "6379:redis"; do
  port="${spec%%:*}"; name="${spec##*:}"
  if lsof -ti ":$port" >/dev/null 2>&1; then
    log "$name already listening on :$port — dev-auto.sh will reuse it, or use scripts/dev-no-docker.sh."
  fi
done

# ── 3. Migrations (best-effort — only if the DB is reachable) ────────────────
VENV_PY="$REPO/src/apps/api/.venv/bin/python"
if [ -x "$VENV_PY" ] && [ -f "$REPO/.env" ]; then
  set -a; # shellcheck source=/dev/null
  source "$REPO/.env"; set +a
  export DATABASE_URL="${DATABASE_URL:-postgresql://postgres:postgres@localhost:5432/nova}"
  if "$VENV_PY" - <<'PY' 2>/dev/null
import os, sys, psycopg2
try:
    psycopg2.connect(os.environ["DATABASE_URL"], connect_timeout=2).close()
except Exception:
    sys.exit(1)
PY
  then
    log "DB reachable — running alembic upgrade head..."
    if (cd "$REPO/src/apps/api" && .venv/bin/alembic upgrade head) >/dev/null 2>&1; then
      log "migrations up to date."
    else
      warn "alembic upgrade failed — run it manually: (cd src/apps/api && .venv/bin/alembic upgrade head)"
    fi
  else
    log "DB not reachable — skipped migrations (dev-auto.sh runs them on start)."
  fi
else
  log "no venv or .env yet — skipped migrations."
fi

log "done. Start the dev env with ./scripts/dev-auto.sh"
