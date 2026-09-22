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

cd ../../.. || exit 1 # repo root (src/apps/web -> repo root)

PATHS=(
  src/apps/web
  src/packages
  package.json
  package-lock.json
)

if git diff --quiet HEAD^ HEAD -- "${PATHS[@]}"; then
  echo "No changes under ${PATHS[*]} -> skipping build"
  exit 0
fi

echo "Changes detected under ${PATHS[*]} -> building"
exit 1
