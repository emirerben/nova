#!/usr/bin/env bash
# Robustness of the background Cinematic bake on the paired iPhone (KRI-1160): backgrounding mid-bake, a kill mid-bake, and Record
# again mid-bake. One run = one scripted Cinematic take plus the interference, through
# TeleprompterHardwareUITests.testTalkBakeRobustness, then a pull of the app container and a check of what it holds.
#
#   scripts/ios/talk-bake-robustness.sh --scenario background --runs 3
#   scripts/ios/talk-bake-robustness.sh --scenario kill --runs 3 --kill-delay 1     # also 3 and 6 to hit the replace window
#   scripts/ios/talk-bake-robustness.sh --scenario retake --runs 3 --skip-build
#   scripts/ios/talk-bake-robustness.sh --scenario relaunch --runs 3   # kill mid-bake, relaunch (KRIA_TALK_BENCH_KEEP_TAKES), reopen the take
#   scripts/ios/talk-bake-robustness.sh --scenario usewait --runs 3    # Use take while the bake runs; pulls the staged upload copy
#   scripts/ios/talk-bake-robustness.sh --scenario previous --runs 3   # retake mid-bake, record take 2, Go back to previous take
#
# Per run it writes .talk-bake/<label>/<scenario>--run<N>/ with result.xcresult, xcodebuild.log, bench/ (Documents/talk-bench,
# the talk.bake.run intervals), teleprompter/ (manifests and movies), tmp-before/after.txt, tmp-staging/ (leftover
# cinematic-<uuid>.mov) and bake-check.json (scripts/ios/talk_bake_check.py: manifest state, ffprobe, a full decode).
#
# Same conventions as talk-bench.sh: Debug build, device-only, opt-in through TEST_RUNNER_KRIA_TELEPROMPTER_HARDWARE_TEST=1 and
# KRIA_TALK_BENCH=1, wait for an unlocked phone, no uninstall between runs (a fresh install breaks the scripted paste). The bench
# wipes KriaTeleprompter and Documents/talk-bench when the app launches with KRIA_TALK_BENCH=1, so every run starts empty, and
# nothing here launches the app again between the test and the pull. The relaunch scenario sets KRIA_TALK_BENCH_KEEP_TAKES=1 and
# KRIA_PERF_DISK_STORE=1 itself so its second launch keeps the take and the chat. The usewait scenario also pulls
# Library/Application Support/KriaUploads (uploads/): the copy Use take staged for the upload queue.
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
IOS_ROOT="$REPO_ROOT/src/apps/ios"
OUT_ROOT="$REPO_ROOT/.talk-bake"
DERIVED_DATA="$IOS_ROOT/.derived-data"
SCHEME="Kria Hardware QA"
TEST_ID="KriaHardwareUITests/TeleprompterHardwareUITests/testTalkBakeRobustness"

SCENARIO=""; RUNS=2; SECONDS_PER_TAKE=30; AWAY=25; KILL_DELAY=1; SECOND_SECONDS=6
LABEL=""; SKIP_BUILD=0; TEST_RETRIES=1
DEVICE_ID="${KRIA_DEVICE_ID:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --scenario) SCENARIO="$2"; shift 2 ;;
    --runs) RUNS="$2"; shift 2 ;;
    --seconds) SECONDS_PER_TAKE="$2"; shift 2 ;;
    --away-seconds) AWAY="$2"; shift 2 ;;
    --kill-delay) KILL_DELAY="$2"; shift 2 ;;
    --second-seconds) SECOND_SECONDS="$2"; shift 2 ;;
    --label) LABEL="$2"; shift 2 ;;
    --device-id) DEVICE_ID="$2"; shift 2 ;;
    --skip-build) SKIP_BUILD=1; shift ;;
    --retry-failed-runs) TEST_RETRIES="$2"; shift 2 ;;
    -h|--help) sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done
[[ "$SCENARIO" =~ ^(background|kill|retake|relaunch|usewait|previous)$ ]] || { echo "--scenario is background, kill, retake, relaunch, usewait or previous" >&2; exit 2; }
[[ -n "$DEVICE_ID" ]] || { echo "Pass --device-id <udid> or set KRIA_DEVICE_ID" >&2; exit 2; }
[[ -n "$LABEL" ]] || LABEL="$(git -C "$REPO_ROOT" rev-parse --short HEAD)-$(date +%Y%m%d-%H%M%S)"
RUN_ROOT="$OUT_ROOT/$LABEL"
mkdir -p "$RUN_ROOT"

BUNDLE_ID="$(sed -n 's/^KRIA_PRODUCT_BUNDLE_IDENTIFIER *= *//p' "$IOS_ROOT/Config/Local.xcconfig" 2>/dev/null | tail -1 | tr -d '[:space:]')"
BUNDLE_ID="${BUNDLE_ID:-com.kria.app.dev}"

device_locked() {
  local json
  json="$(mktemp)"
  xcrun devicectl device info lockState --device "$DEVICE_ID" --json-output "$json" >/dev/null 2>&1 || { rm -f "$json"; echo unknown; return; }
  /usr/bin/python3 -c 'import json,sys; print("locked" if json.load(open(sys.argv[1]))["result"].get("passcodeRequired") else "unlocked")' "$json"
  rm -f "$json"
}

wait_for_unlocked() {
  local deadline=$((SECONDS + 180))
  while [[ "$(device_locked)" == locked ]]; do
    [[ "$SECONDS" -lt "$deadline" ]] || return 1
    echo "talk-bake: the phone is locked; unlock it" >&2
    sleep 5
  done
}

list_tmp() { # <file>: the app's tmp folder (the old staging layout) and tmp/CinematicBake (the current one, in <file minus .txt>-bake.txt)
  xcrun devicectl device info files --device "$DEVICE_ID" --domain-type appDataContainer --domain-identifier "$BUNDLE_ID" --subdirectory tmp >"$1" 2>&1 || true
  xcrun devicectl device info files --device "$DEVICE_ID" --domain-type appDataContainer --domain-identifier "$BUNDLE_ID" --subdirectory tmp/CinematicBake >"${1%.txt}-bake.txt" 2>&1 || true
}

list_uploads() { # <file>: the upload queue's staging folder (what Use take copies a take into)
  xcrun devicectl device info files --device "$DEVICE_ID" --domain-type appDataContainer --domain-identifier "$BUNDLE_ID" --subdirectory "Library/Application Support/KriaUploads" >"$1" 2>&1 || true
}

copy_from() { # <source> <destination> <log>
  xcrun devicectl device copy from --device "$DEVICE_ID" --domain-type appDataContainer --domain-identifier "$BUNDLE_ID" --source "$1" --destination "$2" >>"$3" 2>&1
}

# devicectl may put the files in the destination or in a folder named like the source; flatten either way.
flatten_into() { # <scratch> <inner-name> <dest>
  if [[ -d "$1/$2" ]]; then cp -R "$1/$2"/. "$3"/; elif [[ -d "$1" ]]; then cp -R "$1"/. "$3"/; fi
}

# Use take's staged copy lives only a few seconds when no backend takes the upload (the failed reserve deletes it at once), so
# the usewait scenario copies KriaUploads in a loop while the test runs and keeps any movie it catches in <run-dir>/uploads.
poll_uploads() { # <dest>
  local scratch
  while :; do
    scratch="$(mktemp -d)"
    copy_from "Library/Application Support/KriaUploads" "$scratch" /dev/null || true
    find "$scratch" -name '*.mov' -size +1M -exec cp -n {} "$1"/ \; 2>/dev/null || true
    rm -rf "$scratch"
  done
}

pull_run() { # <run-dir>
  local run_dir="$1" scratch name
  mkdir -p "$run_dir/bench" "$run_dir/teleprompter" "$run_dir/tmp-staging"
  scratch="$(mktemp -d)"
  copy_from "Documents/talk-bench" "$scratch" "$run_dir/devicectl-pull.log" || echo "talk-bake: no Documents/talk-bench" >&2
  flatten_into "$scratch" talk-bench "$run_dir/bench"; rm -rf "$scratch"
  scratch="$(mktemp -d)"
  copy_from "Library/Application Support/KriaTeleprompter" "$scratch" "$run_dir/devicectl-pull.log" || echo "talk-bake: no KriaTeleprompter folder" >&2
  flatten_into "$scratch" KriaTeleprompter "$run_dir/teleprompter"; rm -rf "$scratch"
  mkdir -p "$run_dir/uploads"
  scratch="$(mktemp -d)"
  copy_from "Library/Application Support/KriaUploads" "$scratch" "$run_dir/devicectl-pull.log" || echo "talk-bake: no KriaUploads folder" >&2
  flatten_into "$scratch" KriaUploads "$run_dir/uploads"; rm -rf "$scratch"
  list_uploads "$run_dir/uploads-after.txt"
  list_tmp "$run_dir/tmp-after.txt"
  for name in $(grep -o 'cinematic-[0-9A-Fa-f-]*\.mov' "$run_dir/tmp-after.txt" | sort -u); do
    scratch="$(mktemp -d)"
    copy_from "tmp/$name" "$scratch" "$run_dir/devicectl-pull.log" && cp -R "$scratch/." "$run_dir/tmp-staging/" 2>/dev/null || true
    rm -rf "$scratch"
  done
  for name in $(grep -oE '[0-9A-Fa-f]{8}-[0-9A-Fa-f-]{27}\.mov' "$run_dir/tmp-after-bake.txt" | sort -u); do
    scratch="$(mktemp -d)"
    copy_from "tmp/CinematicBake/$name" "$scratch" "$run_dir/devicectl-pull.log" && cp -R "$scratch/." "$run_dir/tmp-staging/" 2>/dev/null || true
    rm -rf "$scratch"
  done
}

if [[ "$SKIP_BUILD" -eq 0 ]]; then
  bash "$REPO_ROOT/scripts/ios/generate-project.sh"
  xcodebuild build-for-testing -project "$IOS_ROOT/Kria.xcodeproj" -scheme "$SCHEME" -configuration Debug \
    -destination "platform=iOS,id=$DEVICE_ID" -derivedDataPath "$DERIVED_DATA" -allowProvisioningUpdates -enableCodeCoverage NO
fi

echo "talk-bake: label=$LABEL scenario=$SCENARIO runs=$RUNS take=${SECONDS_PER_TAKE}s device=$DEVICE_ID"
for run in $(seq 1 "$RUNS"); do
  run_dir="$RUN_ROOT/$SCENARIO--run$run"
  rm -rf "$run_dir"; mkdir -p "$run_dir"
  attempts=0; exit_code=0
  while :; do
    attempts=$((attempts + 1))
    wait_for_unlocked || { echo "talk-bake: the phone stayed locked for 180 s" >&2; exit 75; }
    list_tmp "$run_dir/tmp-before.txt"
    list_uploads "$run_dir/uploads-before.txt"
    exit_code=0
    poller=""
    if [[ "$SCENARIO" == usewait ]]; then mkdir -p "$run_dir/uploads"; poll_uploads "$run_dir/uploads" & poller=$!; fi
    TEST_RUNNER_KRIA_TELEPROMPTER_HARDWARE_TEST=1 TEST_RUNNER_KRIA_TALK_BENCH=1 \
    TEST_RUNNER_KRIA_TALK_BAKE_SCENARIO="$SCENARIO" TEST_RUNNER_KRIA_TALK_BAKE_SECONDS="$SECONDS_PER_TAKE" \
    TEST_RUNNER_KRIA_TALK_BAKE_AWAY_SECONDS="$AWAY" TEST_RUNNER_KRIA_TALK_BAKE_KILL_DELAY="$KILL_DELAY" \
    TEST_RUNNER_KRIA_TALK_BAKE_SECOND_SECONDS="$SECOND_SECONDS" \
      xcodebuild test-without-building -project "$IOS_ROOT/Kria.xcodeproj" -scheme "$SCHEME" -configuration Debug \
        -destination "platform=iOS,id=$DEVICE_ID" -derivedDataPath "$DERIVED_DATA" -enableCodeCoverage NO \
        -resultBundlePath "$run_dir/result.xcresult" -only-testing "$TEST_ID" >"$run_dir/xcodebuild.log" 2>&1 || exit_code=$?
    if [[ -n "$poller" ]]; then kill "$poller" 2>/dev/null || true; wait "$poller" 2>/dev/null || true; fi
    [[ "$exit_code" -eq 0 ]] && break
    [[ "$attempts" -le "$TEST_RETRIES" ]] || break
    echo "talk-bake:   attempt $attempts failed (exit $exit_code), retrying in 20 s" >&2
    mv "$run_dir/xcodebuild.log" "$run_dir/xcodebuild.attempt$attempts.log"
    mv "$run_dir/result.xcresult" "$run_dir/result.attempt$attempts.xcresult" 2>/dev/null || true
    sleep 20
  done
  grep -q "Test Case .* skipped" "$run_dir/xcodebuild.log" && echo "talk-bake:   the test was skipped" >&2
  pull_run "$run_dir"
  printf '{"scenario": "%s", "xcodebuild_exit": %d, "attempts": %d, "kill_delay": %s, "seconds": %s}\n' \
    "$SCENARIO" "$exit_code" "$attempts" "$KILL_DELAY" "$SECONDS_PER_TAKE" >"$run_dir/status.json"
  python3 "$REPO_ROOT/scripts/ios/talk_bake_check.py" "$run_dir" >/dev/null || true
  echo "talk-bake:   $SCENARIO run $run/$RUNS exit $exit_code after $attempts attempt(s), see $run_dir/bake-check.json"
done
