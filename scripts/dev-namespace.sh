#!/usr/bin/env bash
# Print the deterministic Celery namespace for this checkout.
#
# Worktrees intentionally share .env, Redis, and (often) a database.  The
# absolute checkout path is included in the digest so sibling worktrees whose
# final directory is also named "nova" still get different Redis keyspaces.

set -euo pipefail

REPO="${1:-$(git rev-parse --show-toplevel)}"
REPO="$(cd "$REPO" && pwd -P)"
STEM="$(basename "$REPO" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9_.-' '-')"
STEM="${STEM#-}"
STEM="${STEM%-}"
[[ -n "$STEM" ]] || STEM="nova"

if command -v shasum >/dev/null 2>&1; then
  DIGEST="$(printf '%s' "$REPO" | shasum -a 256 | cut -c1-10)"
else
  # shasum is present on macOS and the supported Linux images.  cksum keeps
  # this helper usable on minimal local shells without making it a hard dep.
  DIGEST="$(printf '%s' "$REPO" | cksum | awk '{print $1}')"
fi

printf 'wt-%s-%s\n' "$STEM" "$DIGEST"

