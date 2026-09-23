#!/usr/bin/env bash

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
IOS_ROOT="$REPO_ROOT/src/apps/ios"
DERIVED_DATA="$IOS_ROOT/.derived-data"
MODE="${KRIA_IOS_TEST_MODE:-full}"
UI_RECEIPT="$DERIVED_DATA/.ci-ui-build"
case "$MODE" in
  full|unit|prepare-ui|ui|boot) ;;
  *) echo "Unknown KRIA_IOS_TEST_MODE: $MODE" >&2; exit 2 ;;
esac

select_simulator() {
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
}

# CI starts this in the background at job start. A fresh runner's first simctl
# call took ~60s and the boot ~65s; run later, both competed with xcodebuild
# startup and compilation. The build phase selects the same device and its
# `bootstatus -b` waits for (or performs) the boot, so this is only a head start.
if [[ "$MODE" == "boot" ]]; then
  SIMULATOR_ID="$(select_simulator)"
  xcrun simctl boot "$SIMULATOR_ID" || echo "Early boot of $SIMULATOR_ID did not start; verify.sh boots it later" >&2
  exit 0
fi

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
  local groups="$1" arguments label="$1"
  # KRIA_IOS_UI_SHARD=i/n runs one part of the full suite; ui_tests.py applies
  # it to both the -only-testing filters and the verified coverage.
  [[ -n "${KRIA_IOS_UI_SHARD:-}" ]] && label="$groups, shard $KRIA_IOS_UI_SHARD"
  arguments="$(python3 "$REPO_ROOT/scripts/ios/ui_tests.py" args "$groups")" || return $?
  local filters=()
  while IFS= read -r argument; do filters+=("$argument"); done <<< "$arguments"
  [[ ${#filters[@]} -gt 0 ]] || return 2
  printf 'UI groups: %s\n%s\n' "$label" "$arguments" | tee "$RESULT_DIR/selection.log"
  # The serial UI suite flakes under simulator/runner contention. Retry inside
  # xcodebuild itself (fail-closed after 3 attempts total); ui_tests.py verify
  # reports any test that only passed on retry as flaky instead of hiding it.
  local xcodebuild_status=0
  timed "UI execution ($label)" xcodebuild "${COMMON_ARGS[@]}" \
    -destination "platform=iOS Simulator,id=$SIMULATOR_ID" \
    -parallel-testing-enabled NO "${filters[@]}" \
    -retry-tests-on-failure -test-iterations 3 \
    -resultBundlePath "$RESULT_DIR/ui.xcresult" test-without-building 2>&1 | tee "$RESULT_DIR/ui.log" \
    || xcodebuild_status=$?
  # Always verify when a result bundle exists, even after a red xcodebuild, so
  # the coverage line and flaky report are produced on exactly the runs where
  # they matter. Without this, `set -e` would abort the function on the line
  # above and verify (and its diagnostics) would never run on a red UI phase.
  local verify_status=0
  if [[ -e "$RESULT_DIR/ui.xcresult" ]]; then
    python3 "$REPO_ROOT/scripts/ios/ui_tests.py" verify "$groups" "$RESULT_DIR/ui.xcresult" \
      | tee -a "$RESULT_DIR/selection.log" || verify_status=$?
  else
    verify_status=1
  fi
  # Fail-closed: both signals must be green for the phase to pass.
  if [[ "$xcodebuild_status" -ne 0 ]]; then
    return "$xcodebuild_status"
  fi
  return "$verify_status"
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

SIMULATOR_ID="$(select_simulator)"

DESTINATION="platform=iOS Simulator,id=$SIMULATOR_ID"

# Boot during compilation instead of paying for startup after the build.
# Runs simctl in the background so the timed shell can forward cancellation.
simctl_wait() {
  local child status=0
  xcrun simctl "$@" &
  child=$!
  trap 'kill "$child" 2>/dev/null || true; wait "$child" 2>/dev/null || true; exit 143' TERM
  wait "$child" || status=$?
  trap - TERM
  return "$status"
}

# True while the selected device is booted or mid-boot (simctl reports both as
# "Booted" once the boot request is accepted).
simulator_boot_in_progress() {
  xcrun simctl list devices -j | /usr/bin/python3 -c '
import json
import sys

udid = sys.argv[1]
payload = json.load(sys.stdin)
for devices in payload.get("devices", {}).values():
    for device in devices:
        if device.get("udid") == udid:
            raise SystemExit(0 if device.get("state") in {"Booted", "Booting"} else 1)
raise SystemExit(1)
' "$SIMULATOR_ID"
}

boot_simulator() {
  local status=0
  simctl_wait bootstatus "$SIMULATOR_ID" -b || status=$?
  if [[ "$status" -ne 0 ]] && simulator_boot_in_progress; then
    # CI's head start (KRIA_IOS_TEST_MODE=boot) may still be booting this
    # device. `bootstatus -b` then races it: it sees a device that is not yet
    # finished, issues its own boot, and simctl refuses with SimError 405
    # "Unable to boot device in current state: Booted" (exit 149; first seen
    # on PR #1183 right after #1181 added the head start). A plain
    # `bootstatus` only monitors the boot already under way, so wait on that
    # instead of failing a green build. A device that is not booting at all
    # keeps the original failure.
    echo "bootstatus -b lost the race with the early boot (exit $status); waiting for it" >&2
    status=0
    simctl_wait bootstatus "$SIMULATOR_ID" || status=$?
  fi
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
# Main's extra UI shards still compile every bundle but leave the unit phase to
# shard 1 on the same commit; the workflow gate requires every shard to pass.
if [[ "$MODE" == "prepare-ui" && "${KRIA_IOS_UNIT_TESTS:-1}" == "0" ]]; then
  echo "Unit execution skipped: another shard runs it for this commit" | tee "$RESULT_DIR/unit.log"
else
  timed "Unit execution (includes runner startup)" xcodebuild "${RUN_TEST_ARGS[@]}" \
    -resultBundlePath "$RESULT_DIR/unit.xcresult" test-without-building 2>&1 | tee "$RESULT_DIR/unit.log"
fi

if [[ "$MODE" == "full" ]]; then
  run_ui full
fi

if [[ "$MODE" == "prepare-ui" ]]; then
  mkdir -p "$DERIVED_DATA"
  CURRENT_INPUTS="$(python3 "$REPO_ROOT/scripts/ios/cache-inputs.py" fingerprint)"
  printf '%s\n%s\n' "$SIMULATOR_ID" "$CURRENT_INPUTS" > "$UI_RECEIPT"
fi
