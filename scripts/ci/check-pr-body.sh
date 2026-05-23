#!/usr/bin/env bash
#
# check-pr-body.sh — verify the PR body satisfies a Lane C gate marker.
#
# Reads the PR body on stdin. The marker shape and skip-marker name are
# passed as args so this script can serve both T6 (local-test) and T8
# (eval-check).
#
# Exit codes:
#   0 — body contains the required marker OR a valid skip-marker
#   1 — body contains neither
#
# Usage:
#   echo "$BODY" | scripts/ci/check-pr-body.sh \
#       --required-regex '^Local test: ([a-f0-9-]{6,})$' \
#       --skip-marker '[skip-local-test]'
#
# The required-regex is an ERE matched against each line of the body. The
# skip-marker requires a justification of at least 10 characters after the
# marker (whitespace-trimmed): `[skip-local-test] regenerating fixtures` ✓,
# `[skip-local-test] short`  ✗.

set -euo pipefail

required_regex=""
skip_marker=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --required-regex)
      required_regex="$2"
      shift 2
      ;;
    --skip-marker)
      skip_marker="$2"
      shift 2
      ;;
    *)
      echo "unknown flag: $1" >&2
      exit 2
      ;;
  esac
done

if [[ -z "$required_regex" || -z "$skip_marker" ]]; then
  echo "usage: $0 --required-regex <ere> --skip-marker <marker>" >&2
  exit 2
fi

body=$(cat)

# Normalize: strip CR (PR bodies from GitHub web UI are CRLF).
body="${body//$'\r'/}"

# Match #1: the required marker (e.g. `Local test: abc123`).
# `grep -E` over the body line-by-line; `-q` suppresses output, `-x` is NOT
# used (we want `^...$` semantics from the caller's regex, not whole-line).
if printf '%s\n' "$body" | grep -qE "$required_regex"; then
  exit 0
fi

# Match #2: skip-marker with ≥10 char justification.
# Escape the marker for regex use (it contains `[` and `]`).
# Builds: `^<escaped-marker> .{10,}$`
escaped_marker=$(printf '%s' "$skip_marker" | sed 's/[][\.*^$/]/\\&/g')
skip_regex="^${escaped_marker} .{10,}\$"

if printf '%s\n' "$body" | grep -qE "$skip_regex"; then
  exit 0
fi

exit 1
