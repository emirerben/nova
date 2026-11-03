#!/usr/bin/env bash
# Talk-to-camera bench on the paired iPhone: live preview, Stop, Use take and editor Save timings, plus the audio facts
# of every take. One run = one scripted take through TeleprompterHardwareUITests.testTalkBenchTake.
#
#   scripts/ios/talk-bench.sh                                    # 3 runs of each default scenario
#   scripts/ios/talk-bench.sh --runs 5 --seconds 20 --label base
#   scripts/ios/talk-bench.sh --scenarios video-cin0,video-cin1  # <none|photo|video>-cin<0|1>, comma separated
#   scripts/ios/talk-bench.sh --dry-run                          # resolve the plan, touch no device and build nothing
#   scripts/ios/talk-bench.sh --pull-only manual                 # report on a session you ran by hand (see below)
#
# HOW IT MEASURES. The app (Debug, KRIA_TALK_BENCH=1) records every Perf signpost interval, count and sample, and the
# engine's, into Documents/talk-bench/summary.json in its container, and keeps each finished take beside it. This script
# pulls that folder with `devicectl`, runs `ffprobe -show_streams` and an ffmpeg showspectrumpic PNG per audio stream on the
# take, and `scripts/ios/talk_bench_report.py` reduces everything to .talk-bench/report.json: median, min, max, MAD and
# run count per timing, per scenario. The scenarios are interleaved (A B C A B C ...), as bench.sh does, so thermal drift
# hits all of them.
#
# WHAT THE SCRIPTED RUN CANNOT DO. "Use take" and editor Save need a signed-in session on a backend that accepts the
# upload; the UI-test fixture does not. Their signposts are in the app and the recorder captures them, but the scripted run
# only fills them if you pass --use-take against a real session. For those two, run the app by hand with the bench on
#   xcrun devicectl device process launch --device <id> --terminate-existing \
#     --environment-variables '{"KRIA_TALK_BENCH":"1"}' <bundle id>
# do the take, Use take and editor Save, then `talk-bench.sh --pull-only <label>`.
#
# The hardware test stays opt-in: it skips unless KRIA_TELEPROMPTER_HARDWARE_TEST=1 and KRIA_TALK_BENCH=1 are both set in
# the test runner's environment, which only this script does. CI and simulators skip it.
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
IOS_ROOT="$REPO_ROOT/src/apps/ios"
OUT_ROOT="$REPO_ROOT/.talk-bench"
DERIVED_DATA="$IOS_ROOT/.derived-data"
REPORT_PY="$REPO_ROOT/scripts/ios/talk_bench_report.py"
SCHEME="Kria Hardware QA"
TEST_ID="KriaHardwareUITests/TeleprompterHardwareUITests/testTalkBenchTake"

RUNS=3
SECONDS_PER_TAKE=10
SCENARIOS="none-cin0,photo-cin0,video-cin0,none-cin1"
DEVICE_ID="${KRIA_DEVICE_ID:-}"
LABEL=""
SKIP_BUILD=0
DRY_RUN=0
USE_TAKE=0
PULL_ONLY=""

usage() { sed -n '2,29p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --runs) RUNS="$2"; shift 2 ;;
    --seconds) SECONDS_PER_TAKE="$2"; shift 2 ;;
    --scenarios) SCENARIOS="$2"; shift 2 ;;
    --device-id) DEVICE_ID="$2"; shift 2 ;;
    --label) LABEL="$2"; shift 2 ;;
    --skip-build) SKIP_BUILD=1; shift ;;
    --use-take) USE_TAKE=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --pull-only) PULL_ONLY="$2"; shift 2 ;;
    -h|--help) usage 0 ;;
    *) echo "Unknown argument: $1" >&2; usage 2 ;;
  esac
done

[[ "$RUNS" =~ ^[1-9][0-9]*$ ]] || { echo "--runs must be a positive integer" >&2; exit 2; }
[[ "$SECONDS_PER_TAKE" =~ ^[1-9][0-9]*$ ]] || { echo "--seconds must be a positive integer" >&2; exit 2; }

SHA="$(git -C "$REPO_ROOT" rev-parse --short HEAD)"
if [[ -n "$(git -C "$REPO_ROOT" status --porcelain -- src/apps/ios)" ]]; then SHA="$SHA-dirty"; fi
[[ -n "$LABEL" ]] || LABEL="$SHA-$(date +%Y%m%d-%H%M%S)"

# Bundle id the app is signed with on this machine (Config/Local.xcconfig), else the dev default.
bundle_id() {
  local value=""
  if [[ -f "$IOS_ROOT/Config/Local.xcconfig" ]]; then
    value="$(sed -n 's/^KRIA_PRODUCT_BUNDLE_IDENTIFIER *= *//p' "$IOS_ROOT/Config/Local.xcconfig" | tail -1 | tr -d '[:space:]')"
  fi
  echo "${value:-com.kria.app.dev}"
}
BUNDLE_ID="$(bundle_id)"

# The first available, paired iPhone, unless --device-id / KRIA_DEVICE_ID names one.
detect_device() {
  local json
  json="$(mktemp)"
  if ! xcrun devicectl list devices --json-output "$json" >/dev/null 2>&1; then rm -f "$json"; return 1; fi
  /usr/bin/python3 - "$json" <<'PY'
import json, sys
devices = json.load(open(sys.argv[1])).get("result", {}).get("devices", [])
for device in devices:
    props, hardware = device.get("deviceProperties", {}), device.get("hardwareProperties", {})
    if hardware.get("platform") == "iOS" and device.get("connectionProperties", {}).get("pairingState") == "paired" \
            and props.get("name") and hardware.get("udid"):
        print(hardware["udid"]); break
PY
  local status=$?
  rm -f "$json"
  return $status
}

if [[ -z "$DEVICE_ID" ]]; then DEVICE_ID="$(detect_device || true)"; fi

IFS=',' read -ra SCENARIO_LIST <<<"$SCENARIOS"
for scenario in "${SCENARIO_LIST[@]}"; do
  [[ "$scenario" =~ ^(none|photo|video)-cin[01]$ ]] || { echo "Bad scenario '$scenario'; expected <none|photo|video>-cin<0|1>" >&2; exit 2; }
done

RUN_ROOT="$OUT_ROOT/$LABEL"

scenario_env() { # scenario -> "background cinematic"
  local background="${1%%-*}" cinematic="${1##*-cin}"
  echo "$background $cinematic"
}

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "talk-bench dry run (no build, no device access)"
  echo "  label:       $LABEL"
  echo "  sha:         $SHA"
  echo "  device:      ${DEVICE_ID:-<none paired and available; a real run would stop here>}"
  echo "  bundle id:   $BUNDLE_ID"
  echo "  scheme/test: $SCHEME / $TEST_ID"
  echo "  derived data:$DERIVED_DATA"
  echo "  runs:        $RUNS per scenario, ${SECONDS_PER_TAKE}s recorded each, use-take=$USE_TAKE"
  echo "  output:      $RUN_ROOT  (report: $OUT_ROOT/report.json)"
  echo "  signing:     $([[ -f "$IOS_ROOT/Config/Local.xcconfig" ]] && echo "Config/Local.xcconfig present" || echo "Config/Local.xcconfig MISSING (see docs/runbooks/ios-development.md)")"
  for tool in xcodebuild xcrun xcodegen ffprobe ffmpeg python3; do
    printf '  tool %-10s %s\n' "$tool" "$(command -v "$tool" || echo MISSING)"
  done
  echo "  order (interleaved):"
  index=0
  for run in $(seq 1 "$RUNS"); do
    for scenario in "${SCENARIO_LIST[@]}"; do
      index=$((index + 1))
      read -r background cinematic < <(scenario_env "$scenario")
      printf '    %2d. %-12s run %d  KRIA_TALK_BENCH_BACKGROUND=%s KRIA_TALK_BENCH_CINEMATIC=%s KRIA_TALK_BENCH_SECONDS=%s -> %s/%s--run%d\n' \
        "$index" "$scenario" "$run" "$background" "$cinematic" "$SECONDS_PER_TAKE" "$RUN_ROOT" "$scenario" "$run"
    done
  done
  exit 0
fi

[[ -n "$DEVICE_ID" ]] || { echo "No paired, available iPhone found. Connect one, or pass --device-id <udid> (xcrun devicectl list devices)." >&2; exit 2; }
mkdir -p "$RUN_ROOT"

pull_run() { # <run-dir>
  local run_dir="$1" scratch
  scratch="$(mktemp -d)"
  if xcrun devicectl device copy from --device "$DEVICE_ID" --domain-type appDataContainer --domain-identifier "$BUNDLE_ID" \
      --source Documents/talk-bench --destination "$scratch" >"$run_dir/devicectl-pull.log" 2>&1; then
    # devicectl may put the files in $scratch or in $scratch/talk-bench; flatten either way.
    local source="$scratch"
    [[ -d "$scratch/talk-bench" ]] && source="$scratch/talk-bench"
    cp -R "$source"/. "$run_dir"/
  else
    echo "talk-bench: could not pull Documents/talk-bench ($(tail -1 "$run_dir/devicectl-pull.log"))" >&2
  fi
  rm -rf "$scratch"
}

finish_report() {
  python3 "$REPORT_PY" report --runs "$RUN_ROOT" --out "$OUT_ROOT/report.json" --label "$LABEL" --sha "$SHA" \
    --device "$DEVICE_ID" ${1:+--planned-runs "$1"}
}

if [[ -n "$PULL_ONLY" ]]; then
  run_dir="$RUN_ROOT/manual--run1"
  mkdir -p "$run_dir"
  pull_run "$run_dir"
  python3 "$REPORT_PY" analyze-take "$run_dir" >/dev/null
  finish_report 1
  exit $?
fi

# --- build once ---------------------------------------------------------------------------------------------------
if [[ "$SKIP_BUILD" -eq 0 ]]; then
  bash "$REPO_ROOT/scripts/ios/generate-project.sh"
  xcodebuild build-for-testing -project "$IOS_ROOT/Kria.xcodeproj" -scheme "$SCHEME" -configuration Debug \
    -destination "platform=iOS,id=$DEVICE_ID" -derivedDataPath "$DERIVED_DATA" -allowProvisioningUpdates \
    -enableCodeCoverage NO | { if command -v xcbeautify >/dev/null 2>&1; then xcbeautify --quiet; else cat; fi; }
fi

echo "talk-bench: label=$LABEL device=$DEVICE_ID bundle=$BUNDLE_ID runs=$RUNS scenarios=$SCENARIOS seconds=$SECONDS_PER_TAKE"

# --- interleaved runs ---------------------------------------------------------------------------------------------
for run in $(seq 1 "$RUNS"); do
  for scenario in "${SCENARIO_LIST[@]}"; do
    read -r background cinematic < <(scenario_env "$scenario")
    run_dir="$RUN_ROOT/$scenario--run$run"
    rm -rf "$run_dir"
    mkdir -p "$run_dir"
    echo "talk-bench: $scenario run $run/$RUNS"
    exit_code=0
    TEST_RUNNER_KRIA_TELEPROMPTER_HARDWARE_TEST=1 TEST_RUNNER_KRIA_TALK_BENCH=1 \
    TEST_RUNNER_KRIA_TALK_BENCH_BACKGROUND="$background" TEST_RUNNER_KRIA_TALK_BENCH_CINEMATIC="$cinematic" \
    TEST_RUNNER_KRIA_TALK_BENCH_SECONDS="$SECONDS_PER_TAKE" TEST_RUNNER_KRIA_TALK_BENCH_USE_TAKE="$USE_TAKE" \
      xcodebuild test-without-building -project "$IOS_ROOT/Kria.xcodeproj" -scheme "$SCHEME" -configuration Debug \
        -destination "platform=iOS,id=$DEVICE_ID" -derivedDataPath "$DERIVED_DATA" -enableCodeCoverage NO \
        -resultBundlePath "$run_dir/result.xcresult" -only-testing "$TEST_ID" >"$run_dir/xcodebuild.log" 2>&1 || exit_code=$?
    reason=""
    grep -q "Test Case .* skipped" "$run_dir/xcodebuild.log" && reason="the test was skipped (opt-in flags or an unsupported camera)"
    # A run that never executed leaves the previous run's Documents/talk-bench on the phone; pulling it would report old numbers as new.
    [[ "$exit_code" -ne 0 && -z "$reason" ]] && reason="xcodebuild exited $exit_code, see xcodebuild.log"
    if [[ "$exit_code" -eq 0 && -z "$reason" ]]; then pull_run "$run_dir"; fi
    printf '{"xcodebuild_exit": %d, "reason": "%s"}\n' "$exit_code" "$reason" >"$run_dir/status.json"
    python3 "$REPORT_PY" analyze-take "$run_dir" >/dev/null || true
    echo "talk-bench:   xcodebuild exit $exit_code, $(ls "$run_dir" | grep -c '^take-.*\.\(mov\|mp4\)$' || true) take(s) pulled"
  done
done

finish_report "$RUNS"
