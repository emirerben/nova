#!/usr/bin/env bash
# test-db.sh — give this worktree its own migrated Postgres test database.
#
# Why: tests default to postgresql://postgres:postgres@localhost:5432/nova_test. On a
# Homebrew Postgres the `postgres` role does not exist, so every DB-backed test errors
# with `FATAL: role "postgres" does not exist` and the run is wasted. The KRI-577 audit
# counted that loss in 15 of 54 sessions (Oct 4-11, 2026). This script creates
# nova_<worktree>_test under the current OS user, migrates it, and records the URL in
# .dev/test-db-url, which scripts/check-api.sh and scripts/preship-check.sh export.
#
# Usage:  bash scripts/dev/test-db.sh            # create/migrate + print export line
#         bash scripts/dev/test-db.sh --drop     # drop this worktree's test database
#         eval "$(bash scripts/dev/test-db.sh --export)"   # just the export line
set -uo pipefail

REPO="$(git rev-parse --show-toplevel 2>/dev/null)" || { echo "not a git repo" >&2; exit 2; }
API="$REPO/src/apps/api"
SLUG="$(basename "$REPO" | tr -c 'a-zA-Z0-9\n' '_' | tr 'A-Z' 'a-z' | sed -E 's/^nova_?//')"
NAME="nova_${SLUG:-primary}_test"
NAME="${NAME:0:60}"
USER_NAME="${PGUSER:-$(id -un)}"
HOST="${PGHOST:-localhost}"
PORT="${PGPORT:-5432}"
URL="postgresql://${USER_NAME}@${HOST}:${PORT}/${NAME}"
URL_FILE="$REPO/.dev/test-db-url"

if [ "${1:-}" = "--export" ] && [ -f "$URL_FILE" ]; then
  printf 'export DATABASE_URL=%q\n' "$(cat "$URL_FILE")"
  exit 0
fi

if ! pg_isready -h "$HOST" -p "$PORT" >/dev/null 2>&1; then
  echo "Postgres is not accepting connections on $HOST:$PORT. Start it first (brew services start postgresql@16, or docker-compose up -d db)." >&2
  exit 1
fi

if [ "${1:-}" = "--drop" ]; then
  dropdb -h "$HOST" -p "$PORT" --if-exists "$NAME" && rm -f "$URL_FILE" && echo "dropped $NAME"
  exit $?
fi

if ! psql -h "$HOST" -p "$PORT" -lqt 2>/dev/null | cut -d '|' -f 1 | grep -qw "$NAME"; then
  createdb -h "$HOST" -p "$PORT" "$NAME" || { echo "createdb $NAME failed (is role $USER_NAME allowed to create databases?)" >&2; exit 1; }
  echo "created $NAME"
fi

PY="$API/.venv/bin/python"
[ -x "$PY" ] || { echo "API venv missing: $PY (run scripts/worktree-setup.sh)" >&2; exit 1; }
# Settings need a few required values to construct; mirror tests/conftest.py defaults
# only when unset so a real .env is never overridden.
( cd "$API" \
  && STORAGE_BUCKET="${STORAGE_BUCKET:-nova-test}" STORAGE_PROVIDER="${STORAGE_PROVIDER:-gcs}" \
     REDIS_URL="${REDIS_URL:-redis://localhost:6379}" OPENAI_API_KEY="${OPENAI_API_KEY:-sk-test}" \
     TOKEN_ENCRYPTION_KEY="${TOKEN_ENCRYPTION_KEY:-test-key-not-used-in-unit-tests}" \
     ALLOWED_ORIGINS="${ALLOWED_ORIGINS:-[\"http://localhost:3000\"]}" \
     DATABASE_URL="$URL" "$PY" -m alembic upgrade head ) || { echo "alembic upgrade head failed on $NAME" >&2; exit 1; }

mkdir -p "$REPO/.dev"
printf '%s\n' "$URL" > "$URL_FILE"
echo "test database ready: $NAME  (recorded in .dev/test-db-url)"
printf 'export DATABASE_URL=%q\n' "$URL"
