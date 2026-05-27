#!/usr/bin/env bash
# check_extension_zip_fresh.sh
#
# CI guard: if a PR changes anything that affects the built Chrome extension
# bundle, it MUST also update the prebuilt zip at
# src/apps/web/public/admin/extension/nova-extension.zip AND the sidecar
# extension-info.json in the same PR.
#
# Why: the zip is a committed binary artifact served by /admin/extension/install
# (PR #345). It's built by a MANUAL script (scripts/build-admin-extension-zip.sh).
# When PR #348 fixed the extension's manifest.json without re-running the build
# script, every admin on a fresh machine kept downloading the stale broken zip
# and hitting the exact icon-load error the PR thought it had fixed. This guard
# prevents that exact recurrence.
#
# Escape hatch for edits that genuinely cannot change bundle output (README-only
# tweaks aren't watched, but if you've e.g. changed a comment in build.mjs):
# put [skip-extension-zip-rebuild] in any commit message on the PR.
#
# Local use:  BASE_SHA=origin/main bash scripts/check_extension_zip_fresh.sh

set -euo pipefail

BASE="${BASE_SHA:-origin/main}"
HEAD_REF="${HEAD_SHA:-HEAD}"

# Files whose change implies the built bundle's bytes may have shifted.
# Deliberately excludes src/apps/extension/README.md — docs don't ship in dist/.
WATCHED_PATHS=(
  "src/apps/extension/src/"
  "src/apps/extension/package.json"
  "src/apps/extension/package-lock.json"
  "src/apps/extension/build.mjs"
)

# Files that MUST appear in the same diff when any WATCHED_PATHS file changes.
REQUIRED_PATHS=(
  "src/apps/web/public/admin/extension/nova-extension.zip"
  "src/apps/web/public/admin/extension/extension-info.json"
)

changed_files="$(git diff --name-only "$BASE" "$HEAD_REF" 2>/dev/null || true)"
if [ -z "$changed_files" ]; then
  echo "Extension zip guard: no diff against $BASE — nothing to check."
  exit 0
fi

watched_hits="$(printf '%s\n' "$changed_files" | grep -F -f <(printf '%s\n' "${WATCHED_PATHS[@]}") || true)"
if [ -z "$watched_hits" ]; then
  echo "Extension zip guard: no extension source/build files changed — OK."
  exit 0
fi

# Escape hatch: a commit on the PR opts out for non-semantic edits.
commit_msgs="$(git log --format='%B' "$BASE..$HEAD_REF" 2>/dev/null || true)"
if printf '%s' "$commit_msgs" | grep -qF '[skip-extension-zip-rebuild]'; then
  echo "Extension zip guard: [skip-extension-zip-rebuild] found in a commit message — bypassing."
  echo "Changed extension source files:"
  printf '  %s\n' $watched_hits
  exit 0
fi

missing=()
for required in "${REQUIRED_PATHS[@]}"; do
  if ! printf '%s\n' "$changed_files" | grep -qxF "$required"; then
    missing+=("$required")
  fi
done

if [ "${#missing[@]}" -eq 0 ]; then
  echo "Extension zip guard: extension source changed AND prebuilt zip + sidecar updated — OK."
  echo "Changed extension source files:"
  printf '  %s\n' $watched_hits
  exit 0
fi

cat >&2 <<EOF
Extension zip guard: FAIL

These extension source/build files changed:
$(printf '  %s\n' $watched_hits)

but the prebuilt zip artifact admins actually download is stale — the diff
does NOT include:
$(printf '  %s\n' "${missing[@]}")

Fix: from a fresh worktree, run

  bash scripts/build-admin-extension-zip.sh

and commit the regenerated src/apps/web/public/admin/extension/nova-extension.zip
and extension-info.json alongside your source change.

If this edit genuinely cannot change the built bundle (e.g. a comment-only
tweak to build.mjs), add [skip-extension-zip-rebuild] to a commit message.
EOF
exit 1
