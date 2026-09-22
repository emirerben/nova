#!/usr/bin/env bash
# Vercel Ignored Build Step. Exit 0 => SKIP build. Exit 1 => BUILD.
# CWD is the project's Root Directory (src/apps/web) when Vercel runs this.
#
# Storage-cost rationale: nova is a monorepo where only ~24% of commits to
# main touch the web app (see agents/DECISIONS.md). Without this gate, every
# commit — including pure-API/iOS/docs commits — rebuilds and permanently
# stores a full copy of the Next.js app, which is what blew through Vercel's
# Hobby storage caps.
set -uo pipefail

# Escape hatch: set FORCE_VERCEL_BUILD=1 in Vercel Project Settings ->
# Environment Variables (Production) to force a rebuild even when this
# script would otherwise skip it. Needed when flipping a NEXT_PUBLIC_* flag,
# since those are inlined at build time and won't take effect otherwise.
if [ "${FORCE_VERCEL_BUILD:-}" = "1" ]; then
  echo "FORCE_VERCEL_BUILD=1 -> building"
  exit 1
fi

# NOTE: root package.json / package-lock.json are intentionally NOT watched.
# .github/workflows/release-metadata.yml commits VERSION, CHANGELOG.md,
# package.json, and package-lock.json (root) on every merge to main — that
# is roughly a third of all commits on main. src/apps/web installs from its
# own package.json/package-lock.json (governed by Root Directory), so the
# root manifests are not consulted by this build anyway. Watching them here
# would make this script a no-op for the single biggest bucket of commits
# it exists to skip.
WATCH=(
  src/apps/web
  src/packages
)

cd "$(git rev-parse --show-toplevel)" || exit 1 # repo root

# Diff against the last commit Vercel actually built for this project+branch,
# not HEAD^ — a push can land more than one commit at once, and HEAD^ HEAD
# only inspects the tip commit, silently skipping a build a middle commit
# needed. VERCEL_GIT_PREVIOUS_SHA is only set once an Ignored Build Step is
# configured, and is empty on a branch's first deployment.
BASE="${VERCEL_GIT_PREVIOUS_SHA:-}"
if [ -z "$BASE" ]; then
  echo "VERCEL_GIT_PREVIOUS_SHA unset (first deployment on this branch) -> building"
  exit 1
fi

# Vercel clones with --depth=10; the base commit may have rolled out of that
# window on a branch that has sat idle. Try a shallow deepen, then fail safe
# to building if it's still unreachable rather than guessing.
if ! git cat-file -e "${BASE}^{commit}" 2>/dev/null; then
  git fetch --no-tags --depth=100 origin "$BASE" >/dev/null 2>&1 || true
fi
if ! git cat-file -e "${BASE}^{commit}" 2>/dev/null; then
  echo "base $BASE not reachable in shallow clone -> building (fail safe)"
  exit 1
fi

if git diff --quiet "$BASE" HEAD -- "${WATCH[@]}"; then
  echo "No changes under ${WATCH[*]} between $BASE and $(git rev-parse --short HEAD) -> skipping build"
  exit 0
fi

echo "Changes detected under ${WATCH[*]} -> building"
git diff --name-only "$BASE" HEAD -- "${WATCH[@]}"
exit 1
