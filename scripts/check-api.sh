#!/usr/bin/env bash
# Run focused API diagnostics from any directory without picking up an editable
# install from a different Nova worktree.
set -u

SOURCE_PATH="${BASH_SOURCE[0]}"
while [ -h "$SOURCE_PATH" ]; do
  SOURCE_DIR="$(cd -P "$(dirname "$SOURCE_PATH")" && pwd)"
  LINK_TARGET="$(readlink "$SOURCE_PATH")"
  if [[ "$LINK_TARGET" = /* ]]; then
    SOURCE_PATH="$LINK_TARGET"
  else
    SOURCE_PATH="$SOURCE_DIR/$LINK_TARGET"
  fi
done

ROOT_DIR="$(cd -P "$(dirname "$SOURCE_PATH")/.." && pwd)"
API_DIR="$ROOT_DIR/src/apps/api"
PYTHON_BIN="$API_DIR/.venv/bin/python"

if [ ! -x "$PYTHON_BIN" ]; then
  printf 'API virtual environment is missing: %s\n' "$PYTHON_BIN" >&2
  printf 'Run scripts/worktree-setup.sh from the repository root, then retry.\n' >&2
  exit 127
fi

if ! cd "$API_DIR"; then
  printf 'API directory is missing: %s\n' "$API_DIR" >&2
  exit 1
fi

# A shared editable environment may retain a path from another worktree.
# Imports must resolve from this worktree after the cd above.
unset PYTHONPATH

case "${1:-}" in
  test)
    shift
    exec "$PYTHON_BIN" -m pytest "$@"
    ;;
  lint)
    shift
    if [ "$#" -eq 0 ]; then
      printf 'Usage: %s lint {check|format} [arguments ...]\n' "${BASH_SOURCE[0]}" >&2
      exit 2
    fi
    exec "$PYTHON_BIN" -m ruff "$@"
    ;;
  profile)
    shift
    exec "$PYTHON_BIN" -m app.cli.runtime_profile "$@"
    ;;
  *)
    printf 'Usage: %s {test|profile} [arguments ...] | lint {check|format} [arguments ...]\n' \
      "${BASH_SOURCE[0]}" >&2
    exit 2
    ;;
esac
