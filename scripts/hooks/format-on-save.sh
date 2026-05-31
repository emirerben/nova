#!/usr/bin/env bash
# PostToolUse hook: auto-format the file Claude just edited.
#   .py  under src/apps/api  -> ruff format + ruff check --fix
#   .ts/.tsx under src/apps/web -> prettier --write
# Fail-open by design: any missing tool, unparseable input, or unmatched path
# exits 0 so it can NEVER block or slow an edit. Reads the PostToolUse JSON on stdin.

set -uo pipefail

INPUT="$(cat 2>/dev/null || true)"
[ -z "$INPUT" ] && exit 0

# Extract tool_input.file_path with stdlib python (no jq dependency).
FILE="$(printf '%s' "$INPUT" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    print((d.get("tool_input") or {}).get("file_path", ""))
except Exception:
    pass
' 2>/dev/null || true)"

[ -z "$FILE" ] && exit 0
[ -f "$FILE" ] || exit 0

ROOT="$(git -C "$(dirname "$FILE")" rev-parse --show-toplevel 2>/dev/null || true)"
[ -z "$ROOT" ] && exit 0

case "$FILE" in
  "$ROOT"/src/apps/api/*.py)
    if command -v ruff >/dev/null 2>&1; then
      ruff format "$FILE" >/dev/null 2>&1 || true
      ruff check --fix "$FILE" >/dev/null 2>&1 || true
    elif [ -x "$ROOT/src/apps/api/.venv/bin/ruff" ]; then
      "$ROOT/src/apps/api/.venv/bin/ruff" format "$FILE" >/dev/null 2>&1 || true
      "$ROOT/src/apps/api/.venv/bin/ruff" check --fix "$FILE" >/dev/null 2>&1 || true
    fi
    ;;
  "$ROOT"/src/apps/web/*.ts|"$ROOT"/src/apps/web/*.tsx)
    if [ -x "$ROOT/src/apps/web/node_modules/.bin/prettier" ]; then
      "$ROOT/src/apps/web/node_modules/.bin/prettier" --write "$FILE" >/dev/null 2>&1 || true
    fi
    ;;
esac

exit 0
