#!/usr/bin/env bash

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
IOS_ROOT="$REPO_ROOT/src/apps/ios"

if ! command -v xcodegen >/dev/null 2>&1; then
  echo "xcodegen is required. Install it with: brew install xcodegen" >&2
  exit 2
fi

cd "$IOS_ROOT"
xcodegen generate --spec project.yml
echo "Generated $IOS_ROOT/Kria.xcodeproj"
