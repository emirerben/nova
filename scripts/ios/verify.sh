#!/usr/bin/env bash

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
IOS_ROOT="$REPO_ROOT/src/apps/ios"
DERIVED_DATA="$IOS_ROOT/.derived-data"

"$REPO_ROOT/scripts/ios/generate-project.sh"

cd "$IOS_ROOT"

assert_api_base_url() {
  local configuration="$1"
  local expected="$2"
  local actual
  actual="$({
    xcodebuild \
      -project Kria.xcodeproj \
      -scheme Kria \
      -configuration "$configuration" \
      -showBuildSettings 2>/dev/null
  } | awk -F ' = ' '/^[[:space:]]*API_BASE_URL = / { print $2; exit }')"
  if [[ "$actual" != "$expected" ]]; then
    echo "Expected $configuration API_BASE_URL=$expected, got ${actual:-<unset>}" >&2
    exit 1
  fi
}

# In xcconfig files, an unescaped // starts a comment and silently truncates
# https:// URLs to https:. Pin the resolved build setting, not just the source.
assert_api_base_url Staging "https://staging.usekria.com"
assert_api_base_url Release "https://nova-video.fly.dev"

xcodebuild \
  -project Kria.xcodeproj \
  -scheme Kria \
  -skipPackagePluginValidation \
  -destination "generic/platform=iOS Simulator" \
  -derivedDataPath "$DERIVED_DATA" \
  CODE_SIGNING_ALLOWED=NO \
  build

if [[ "${KRIA_SKIP_SIMULATOR_TESTS:-0}" == "1" ]]; then
  echo "Skipping simulator tests because KRIA_SKIP_SIMULATOR_TESTS=1"
  exit 0
fi

SIMULATOR_ID="$(
  xcrun simctl list devices available -j | /usr/bin/python3 -c '
import json
import sys

payload = json.load(sys.stdin)
for runtime, devices in reversed(list(payload.get("devices", {}).items())):
    if "iOS" not in runtime:
        continue
    for device in devices:
        if device.get("isAvailable") and device.get("name", "").startswith("iPhone"):
            print(device["udid"])
            raise SystemExit(0)
raise SystemExit("No available iPhone simulator found")
'
)"

xcodebuild \
  -project Kria.xcodeproj \
  -scheme Kria \
  -skipPackagePluginValidation \
  -destination "platform=iOS Simulator,id=$SIMULATOR_ID" \
  -derivedDataPath "$DERIVED_DATA" \
  CODE_SIGNING_ALLOWED=NO \
  test
