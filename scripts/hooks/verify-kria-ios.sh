#!/usr/bin/env bash
# Local agent hook: enable Git hooks, enqueue committed native changes, show results.
# No simulator work runs in this foreground process. Usage: ... session|finish|pr
set -u
[[ "$(uname -s)" == Darwin ]] || exit 0
repo="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
script="$repo/scripts/ios/auto-verify.py"
[[ -f "$script" ]] || exit 0
cd "$repo" || exit 0
python3 "$script" install >/dev/null || exit 0
python3 "$script" enqueue --reason "${1:-session}" || true
python3 "$script" status || true
exit 0
