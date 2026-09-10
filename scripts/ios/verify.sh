#!/usr/bin/env bash

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
IOS_ROOT="$REPO_ROOT/src/apps/ios"
DERIVED_DATA="$IOS_ROOT/.derived-data"
MODE="${KRIA_IOS_TEST_MODE:-full}"
UI_RECEIPT="$DERIVED_DATA/.ci-ui-build"
case "$MODE" in
  full|unit|prepare-ui|ui) ;;
  *) echo "Unknown KRIA_IOS_TEST_MODE: $MODE" >&2; exit 2 ;;
esac

# CI's UI phase reuses exactly the app/test build that already passed unit tests.
# Keep the receipt outside cached Build data so a cache hit cannot authorize it.
if [[ "$MODE" == "ui" ]]; then
  [[ -f "$UI_RECEIPT" ]] || { echo "Run prepare-ui successfully before ui" >&2; exit 2; }
  SIMULATOR_ID="$(sed -n '1p' "$UI_RECEIPT")"
  BUILT_INPUTS="$(sed -n '2p' "$UI_RECEIPT")"
  CURRENT_INPUTS="$(python3 "$REPO_ROOT/scripts/ios/cache-inputs.py" fingerprint)"
  [[ -n "$SIMULATOR_ID" && "$BUILT_INPUTS" == "$CURRENT_INPUTS" ]] || {
    echo "iOS inputs changed after build; run prepare-ui again" >&2; exit 2;
  }
  rm "$UI_RECEIPT"
  cd "$IOS_ROOT"
  xcodebuild -project Kria.xcodeproj -scheme Kria -skipPackagePluginValidation \
    -derivedDataPath "$DERIVED_DATA" CODE_SIGNING_ALLOWED=NO \
    -destination "platform=iOS Simulator,id=$SIMULATOR_ID" \
    -parallel-testing-enabled NO -only-testing:KriaUITests test-without-building
  exit 0
fi

rm -f "$UI_RECEIPT"

"$REPO_ROOT/scripts/ios/generate-project.sh"

if [[ "${KRIA_RESTORE_INPUT_TIMES:-0}" == "1" ]]; then
  python3 "$REPO_ROOT/scripts/ios/cache-inputs.py" restore
fi

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

COMMON_ARGS=(
  -project Kria.xcodeproj
  -scheme Kria
  -skipPackagePluginValidation
  -derivedDataPath "$DERIVED_DATA"
  CODE_SIGNING_ALLOWED=NO
)

if [[ "${KRIA_SKIP_SIMULATOR_TESTS:-0}" == "1" ]]; then
  xcodebuild "${COMMON_ARGS[@]}" -destination "generic/platform=iOS Simulator" build
  echo "Skipping simulator tests because KRIA_SKIP_SIMULATOR_TESTS=1"
  exit 0
fi

SIMULATOR_ID="$(
  xcrun simctl list devices available -j | /usr/bin/python3 -c '
import json
import os
import sys

requested = os.environ.get("KRIA_SIMULATOR_ID", "")
payload = json.load(sys.stdin)
for runtime, devices in reversed(list(payload.get("devices", {}).items())):
    if "iOS" not in runtime:
        continue
    for device in devices:
        if device.get("isAvailable") and device.get("name", "").startswith("iPhone"):
            if requested and device["udid"] != requested:
                continue
            print(device["udid"])
            raise SystemExit(0)
raise SystemExit("Requested iPhone simulator is unavailable" if requested else "No available iPhone simulator found")
'
)"

DESTINATION="platform=iOS Simulator,id=$SIMULATOR_ID"

# Boot during compilation instead of paying for startup after the build.
xcrun simctl bootstatus "$SIMULATOR_ID" -b &
BOOT_PID=$!
trap 'kill "$BOOT_PID" 2>/dev/null || true' EXIT

# Unit-only changes do not need to compile the UI runner. prepare-ui compiles
# both bundles once; the following UI step reuses them without another build.
BUILD_TEST_ARGS=("${COMMON_ARGS[@]}" -destination "$DESTINATION")
RUN_TEST_ARGS=("${COMMON_ARGS[@]}" -destination "$DESTINATION" -parallel-testing-enabled NO)
if [[ "$MODE" == "unit" ]]; then
  BUILD_TEST_ARGS+=(-only-testing:KriaTests)
fi
if [[ "$MODE" == "unit" || "$MODE" == "prepare-ui" ]]; then
  RUN_TEST_ARGS+=(-only-testing:KriaTests)
fi
xcodebuild "${BUILD_TEST_ARGS[@]}" build-for-testing
wait "$BOOT_PID"
trap - EXIT

# Keep UI execution serial: cloned parallel runners can miss drawer controls.
xcodebuild "${RUN_TEST_ARGS[@]}" test-without-building

if [[ "$MODE" == "prepare-ui" ]]; then
  mkdir -p "$DERIVED_DATA"
  CURRENT_INPUTS="$(python3 "$REPO_ROOT/scripts/ios/cache-inputs.py" fingerprint)"
  printf '%s\n%s\n' "$SIMULATOR_ID" "$CURRENT_INPUTS" > "$UI_RECEIPT"
fi
