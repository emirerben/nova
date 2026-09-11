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

# Each invocation keeps independent bundles/logs, including failed runs.
RESULT_ROOT="$REPO_ROOT/test-results/ios"
mkdir -p "$RESULT_ROOT"
RESULT_DIR="$(mktemp -d "$RESULT_ROOT/$MODE.XXXXXX")"

timed() {
  local phase="$1" started=$SECONDS status=0
  shift
  "$@" || status=$?
  local elapsed=$((SECONDS - started))
  printf '%s: %ss (exit %s)\n' "$phase" "$elapsed" "$status" | tee -a "$RESULT_DIR/timings.log"
  if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    printf -- '- %s: %ss (exit %s)\n' "$phase" "$elapsed" "$status" >> "$GITHUB_STEP_SUMMARY"
  fi
  return "$status"
}

run_ui() {
  local groups="$1" arguments
  arguments="$(python3 "$REPO_ROOT/scripts/ios/ui_tests.py" args "$groups")" || return $?
  local filters=()
  while IFS= read -r argument; do filters+=("$argument"); done <<< "$arguments"
  [[ ${#filters[@]} -gt 0 ]] || return 2
  printf 'UI groups: %s\n%s\n' "$groups" "$arguments" | tee "$RESULT_DIR/selection.log"
  timed "UI execution ($groups)" xcodebuild "${COMMON_ARGS[@]}" \
    -destination "platform=iOS Simulator,id=$SIMULATOR_ID" \
    -parallel-testing-enabled NO "${filters[@]}" \
    -resultBundlePath "$RESULT_DIR/ui.xcresult" test-without-building 2>&1 | tee "$RESULT_DIR/ui.log"
  python3 "$REPO_ROOT/scripts/ios/ui_tests.py" verify "$groups" "$RESULT_DIR/ui.xcresult" | tee -a "$RESULT_DIR/selection.log"
}

COMMON_ARGS=(
  -project Kria.xcodeproj
  -scheme Kria
  -skipPackagePluginValidation
  -derivedDataPath "$DERIVED_DATA"
  CODE_SIGNING_ALLOWED=NO
)

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
  run_ui "${KRIA_IOS_UI_GROUPS-}"
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
      -derivedDataPath "$DERIVED_DATA" \
      -skipPackagePluginValidation \
      -configuration "$configuration" \
      -showBuildSettings 2>"$RESULT_DIR/$configuration-build-settings.log"
  } | awk -F ' = ' '/^[[:space:]]*API_BASE_URL = / && !seen { print $2; seen=1 }')" || return $?
  if [[ "$expected" == "valid-url" ]]; then
    # Local.xcconfig may intentionally point Debug at another development host.
    # Still reject the truncated `http:` value that made every chat request fail.
    if [[ ! "$actual" =~ ^https?://[^/[:space:]]+ ]]; then
      echo "Expected $configuration to resolve a complete HTTP(S) API URL, got ${actual:-<unset>}" >&2
      return 1
    fi
    return
  fi
  if [[ "$actual" != "$expected" ]]; then
    echo "Expected $configuration API_BASE_URL=$expected, got ${actual:-<unset>}" >&2
    return 1
  fi
}

# In xcconfig files, an unescaped // starts a comment and silently truncates
# https:// URLs to https:. Pin the resolved build setting, not just the source.
timed "Debug build settings" assert_api_base_url Debug "valid-url"
timed "Staging build settings" assert_api_base_url Staging "https://staging.usekria.com"
timed "Release build settings" assert_api_base_url Release "https://nova-video.fly.dev"


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
boot_simulator() {
  local child status=0
  xcrun simctl bootstatus "$SIMULATOR_ID" -b &
  child=$!
  # The timed background shell must forward cancellation to simctl.
  trap 'kill "$child" 2>/dev/null || true; wait "$child" 2>/dev/null || true; exit 143' TERM
  wait "$child" || status=$?
  trap - TERM
  return "$status"
}
timed "Simulator readiness (overlaps compilation)" boot_simulator > "$RESULT_DIR/simulator.log" 2>&1 &
BOOT_PID=$!
trap 'kill "$BOOT_PID" 2>/dev/null || true; wait "$BOOT_PID" 2>/dev/null || true' EXIT

# Filter out UI execution for the fast phase while retaining any other test
# targets. prepare-ui builds the full scheme once for the following UI step.
BUILD_TEST_ARGS=("${COMMON_ARGS[@]}" -destination "$DESTINATION")
RUN_TEST_ARGS=("${COMMON_ARGS[@]}" -destination "$DESTINATION" -parallel-testing-enabled NO)
if [[ "$MODE" == "unit" ]]; then
  BUILD_TEST_ARGS+=(-skip-testing:KriaUITests)
fi
RUN_TEST_ARGS+=(-skip-testing:KriaUITests)
timed "Compilation" xcodebuild "${BUILD_TEST_ARGS[@]}" build-for-testing 2>&1 | tee "$RESULT_DIR/build.log"
timed "Simulator wait after compilation" wait "$BOOT_PID"
trap - EXIT

# Keep UI execution serial: cloned parallel runners can miss drawer controls.
timed "Unit execution (includes runner startup)" xcodebuild "${RUN_TEST_ARGS[@]}" \
  -resultBundlePath "$RESULT_DIR/unit.xcresult" test-without-building 2>&1 | tee "$RESULT_DIR/unit.log"

if [[ "$MODE" == "full" ]]; then
  run_ui full
fi

if [[ "$MODE" == "prepare-ui" ]]; then
  mkdir -p "$DERIVED_DATA"
  CURRENT_INPUTS="$(python3 "$REPO_ROOT/scripts/ios/cache-inputs.py" fingerprint)"
  printf '%s\n%s\n' "$SIMULATOR_ID" "$CURRENT_INPUTS" > "$UI_RECEIPT"
fi
