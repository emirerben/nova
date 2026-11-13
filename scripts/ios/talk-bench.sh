#!/usr/bin/env bash
# Talk-to-camera bench on the paired iPhone: live preview, Stop, Use take and editor Save timings, plus the audio facts
# of every take. One run = one scripted take through TeleprompterHardwareUITests.testTalkBenchTake.
#
#   scripts/ios/talk-bench.sh                                    # 3 runs of each default scenario
#   scripts/ios/talk-bench.sh --runs 5 --seconds 20 --idle-seconds 60 --label base
#   scripts/ios/talk-bench.sh --scenarios video-cin0,video-cin1  # <none|photo|video>-cin<0|1>, comma separated
#   scripts/ios/talk-bench.sh --scenarios video-cin0,video-cin0-old   # A/B the live-preview levers (see below)
#   scripts/ios/talk-bench.sh --dry-run                          # resolve the plan, touch no device and build nothing
#   scripts/ios/talk-bench.sh --pull-only manual                 # report on a session you ran by hand (see below)
#   scripts/ios/talk-bench.sh --use-take --use-take-dwell 20 --use-take-after 12 --poll-files   # dwell in Review, then Use take
#   scripts/ios/talk-bench.sh --retries 2 --retry-failed-runs 2  # install/launch retries, and reruns of a failed measurement
#
# HOW IT MEASURES. The app (Debug, KRIA_TALK_BENCH=1) records every Perf signpost interval, count and sample, and the
# engine's, into Documents/talk-bench/summary.json in its container, and keeps each finished take beside it. This script
# pulls that folder with `devicectl`, runs `ffprobe -show_streams` and an ffmpeg showspectrumpic PNG per audio stream on the
# take, and `scripts/ios/talk_bench_report.py` reduces everything to .talk-bench/report.json: median, min, max, MAD and
# run count per timing, per scenario. The scenarios are interleaved (A B C A B C ...), as bench.sh does, so thermal drift
# hits all of them.
#
# LIVE-PREVIEW A/B. A scenario may end in `-old` (all five live-preview levers back to their previous behaviour) or in
# `-old-<lever>[-<lever>...]` with lever one of fresh, input, async, video, decode (only those). The Debug app reads this as
# KRIA_TALK_BENCH_LIVE_OLD and writes the resulting values to the `live.tuning` fact of the report. The scenarios are
# interleaved, so `video-cin0,video-cin0-old` is one A/B under the same thermal drift. Needs a background (photo or video).
#
# USE TAKE DWELL. --use-take-dwell N waits N seconds in Review before tapping Use take (what the creator spends deciding; the take
# prewarm works during it), --use-take-after N keeps the app open N seconds after the tap. --poll-files copies the prewarm proxy
# (Library/Caches/KriaTakePrewarm) and the staged upload (Library/Application Support/KriaUploads) off the phone while the test
# runs and keeps them in <run>/polled/, because the fixture's failed upload deletes the staged copy within seconds.
#
# WHAT THE SCRIPTED RUN CANNOT DO. "Use take" and editor Save need a signed-in session on a backend that accepts the
# upload; the UI-test fixture does not. Their signposts are in the app and the recorder captures them, but the scripted run
# only fills them if you pass --use-take against a real session. For those two, run the app by hand with the bench on
#   xcrun devicectl device process launch --device <id> --terminate-existing \
#     --environment-variables '{"KRIA_TALK_BENCH":"1"}' <bundle id>
# do the take, Use take and editor Save, then `talk-bench.sh --pull-only <label>`.
#
# DEVICE HYGIENE. Before every run the script checks the phone is unlocked (and waits for it, a locked phone fails the
# install with a launch error), and uninstalls the app: a run leaves a 60-80 MB take in the app's container and 7 runs
# filled the phone until the next install failed with "Insufficient storage". The uninstall also resets the Cinematic
# preference, which persists per device, so every run starts from the same state. An install or launch failure is
# retried up to --retries times (--resume keeps the good runs already in the label's folder); a test that ran and failed is retried only with --retry-failed-runs. The transport
# (wired or localNetwork) is logged in each run's status.json, because an install over Wi-Fi is slower and flakier.
#
# THERMAL. A phone that starts a run at "serious" or "critical" is throttled: the same take cost 42 s to build a 10 s test video
# that takes seconds when cool. Before each run the script launches the installed app with the bench on, reads
# ProcessInfo.thermalState from its summary, and waits (polling once a minute, at most --cooldown-max seconds, default 900)
# until the state is nominal or fair. The state it started in is in the report (thermal.start/end/max) whatever happens.
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
IDLE_SECONDS=6
REVIEW_TIMEOUT=240
SCENARIOS="none-cin0,photo-cin0,video-cin0,none-cin1"
DEVICE_ID="${KRIA_DEVICE_ID:-}"
LABEL=""
SKIP_BUILD=0
COOLDOWN_MAX=900
RETRIES=2
RESUME=0
REINSTALL=0
TEST_RETRIES=0
DRY_RUN=0
USE_TAKE=0
USE_TAKE_DWELL=0
USE_TAKE_AFTER=45
POLL_FILES=0
PULL_ONLY=""

usage() { sed -n '2,45p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --runs) RUNS="$2"; shift 2 ;;
    --seconds) SECONDS_PER_TAKE="$2"; shift 2 ;;
    --idle-seconds) IDLE_SECONDS="$2"; shift 2 ;;
    --review-timeout) REVIEW_TIMEOUT="$2"; shift 2 ;;
    --scenarios) SCENARIOS="$2"; shift 2 ;;
    --device-id) DEVICE_ID="$2"; shift 2 ;;
    --label) LABEL="$2"; shift 2 ;;
    --skip-build) SKIP_BUILD=1; shift ;;
    --retries) RETRIES="$2"; shift 2 ;;
    --resume) RESUME=1; shift ;;
    --reinstall) REINSTALL=1; shift ;;
    --cooldown-max) COOLDOWN_MAX="$2"; shift 2 ;;
    --retry-failed-runs) TEST_RETRIES="$2"; shift 2 ;;
    --use-take) USE_TAKE=1; shift ;;
    --use-take-dwell) USE_TAKE_DWELL="$2"; shift 2 ;;
    --use-take-after) USE_TAKE_AFTER="$2"; shift 2 ;;
    --poll-files) POLL_FILES=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --pull-only) PULL_ONLY="$2"; shift 2 ;;
    -h|--help) usage 0 ;;
    *) echo "Unknown argument: $1" >&2; usage 2 ;;
  esac
done

[[ "$RUNS" =~ ^[1-9][0-9]*$ ]] || { echo "--runs must be a positive integer" >&2; exit 2; }
[[ "$SECONDS_PER_TAKE" =~ ^[1-9][0-9]*$ ]] || { echo "--seconds must be a positive integer" >&2; exit 2; }
[[ "$RETRIES" =~ ^[0-9]+$ && "$TEST_RETRIES" =~ ^[0-9]+$ ]] || { echo "--retries and --retry-failed-runs must be non-negative integers" >&2; exit 2; }

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
  [[ "$scenario" =~ ^(none|photo|video)-cin[01](-old(-(fresh|input|async|video|decode))*)?$ ]] || {
    echo "Bad scenario '$scenario'; expected <none|photo|video>-cin<0|1>[-old[-fresh][-input][-async][-video][-decode]]" >&2; exit 2; }
done

RUN_ROOT="$OUT_ROOT/$LABEL"

scenario_env() { # scenario -> "background cinematic live-old" (live-old is "-" for none, "all", or a comma list)
  local background="${1%%-*}" rest="${1#*-cin}" old="-"
  local cinematic="${rest:0:1}"
  if [[ "$rest" == *-old* ]]; then
    old="${rest#*-old}"; old="${old#-}"; old="${old//-/,}"
    [[ -n "$old" ]] || old="all"
  fi
  echo "$background $cinematic $old"
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
      read -r background cinematic live_old < <(scenario_env "$scenario")
      printf '    %2d. %-18s run %d  KRIA_TALK_BENCH_BACKGROUND=%s KRIA_TALK_BENCH_CINEMATIC=%s KRIA_TALK_BENCH_LIVE_OLD=%s KRIA_TALK_BENCH_SECONDS=%s -> %s/%s--run%d\n' \
        "$index" "$scenario" "$run" "$background" "$cinematic" "${live_old/#-/}" "$SECONDS_PER_TAKE" "$RUN_ROOT" "$scenario" "$run"
    done
  done
  exit 0
fi

[[ -n "$DEVICE_ID" ]] || { echo "No paired, available iPhone found. Connect one, or pass --device-id <udid> (xcrun devicectl list devices)." >&2; exit 2; }
mkdir -p "$RUN_ROOT"

poll_files() { # <dest>: keep every proxy and staged movie that shows up while the test runs
  local dest="$1" scratch directory
  mkdir -p "$dest/prewarm" "$dest/uploads"
  while :; do
    for directory in prewarm:Library/Caches/KriaTakePrewarm uploads:"Library/Application Support/KriaUploads"; do
      scratch="$(mktemp -d)"
      xcrun devicectl device copy from --device "$DEVICE_ID" --domain-type appDataContainer --domain-identifier "$BUNDLE_ID" \
        --source "${directory#*:}" --destination "$scratch" >/dev/null 2>&1 || true
      find "$scratch" -type f -size +100k -exec cp -n {} "$dest/${directory%%:*}"/ \; 2>/dev/null || true
      rm -rf "$scratch"
    done
  done
}

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


# --- device hygiene -----------------------------------------------------------------------------------------------
device_json() { # <devicectl device info subcommand> -> JSON path on stdout, empty when the call failed
  local out
  out="$(mktemp)"
  if xcrun devicectl device info "$1" --device "$DEVICE_ID" --json-output "$out" >/dev/null 2>&1; then echo "$out"; else rm -f "$out"; fi
}

device_transport() {
  local json
  json="$(device_json details)"
  [[ -n "$json" ]] || { echo unknown; return; }
  /usr/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["result"]["connectionProperties"].get("transportType","unknown"))' "$json" 2>/dev/null || echo unknown
  rm -f "$json"
}

# Prints "locked", "unlocked" or "unknown". passcodeRequired is true while the phone needs its passcode.
device_lock_state() {
  local json
  json="$(device_json lockState)"
  [[ -n "$json" ]] || { echo unknown; return; }
  /usr/bin/python3 -c 'import json,sys; r=json.load(open(sys.argv[1]))["result"]; print("locked" if r.get("passcodeRequired") else "unlocked")' "$json" 2>/dev/null || echo unknown
  rm -f "$json"
}

# Waits up to $1 seconds for an unlocked phone. Launching the app is the only wake devicectl offers, and it works only while
# the phone is unlocked, so a locked phone has to be unlocked by hand; the script says so and keeps polling.
wait_for_unlocked_device() {
  local deadline=$((SECONDS + $1)) state announced=0
  while :; do
    state="$(device_lock_state)"
    [[ "$state" != "locked" ]] && return 0
    if [[ "$announced" -eq 0 ]]; then echo "talk-bench: the phone is locked; unlock it (waiting up to $1 s)" >&2; announced=1; fi
    xcrun devicectl device process launch --device "$DEVICE_ID" "$BUNDLE_ID" >/dev/null 2>&1 || true
    [[ "$SECONDS" -lt "$deadline" ]] || return 1
    sleep 5
  done
}

# Another xcodebuild or devicectl holding the same phone makes the install fail; a simulator build elsewhere does not.
competing_device_users() {
  pgrep -fl "xcodebuild|devicectl" 2>/dev/null | grep -F -e "$DEVICE_ID" -e "00008110" | grep -v "$$" || true
}

# Thermal state of the phone as the app sees it, by launching the installed app for a few seconds with the bench on.
# Prints nominal, fair, serious, critical, or unknown (no app installed yet, or the launch failed).
probe_thermal() {
  local scratch state
  xcrun devicectl device process launch --device "$DEVICE_ID" --terminate-existing \
    --environment-variables '{"KRIA_TALK_BENCH":"1"}' "$BUNDLE_ID" >/dev/null 2>&1 || { echo unknown; return; }
  sleep 7
  scratch="$(mktemp -d)"
  state=unknown
  if xcrun devicectl device copy from --device "$DEVICE_ID" --domain-type appDataContainer --domain-identifier "$BUNDLE_ID" \
      --source Documents/talk-bench/summary.json --destination "$scratch/summary.json" >/dev/null 2>&1; then
    state="$(/usr/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["facts"].get("thermal.end","unknown"))' "$scratch/summary.json" 2>/dev/null || echo unknown)"
  fi
  rm -rf "$scratch"
  echo "$state"
}

# Waits for nominal or fair. Returns the last state seen on stdout; never fails the run, because a hot phone is data too.
cool_down_device() {
  local deadline=$((SECONDS + COOLDOWN_MAX)) state
  state="$(probe_thermal)"
  while [[ "$state" == serious || "$state" == critical ]]; do
    echo "talk-bench: phone is $state; cooling (up to $((deadline - SECONDS)) s more)" >&2
    [[ "$SECONDS" -lt "$deadline" ]] || { echo "talk-bench: WARNING still $state after ${COOLDOWN_MAX}s, running anyway" >&2; break; }
    sleep 60
    state="$(probe_thermal)"
  done
  echo "$state"
}

uninstall_app() {
  xcrun devicectl device uninstall app --device "$DEVICE_ID" "$BUNDLE_ID" >/dev/null 2>&1 || true
}

# Matches what the log says when the run never got as far as the test body.
infrastructure_failure() { # <log>
  grep -q -E "Failed to install the app on the device|Failed to get launch progress|Insufficient storage|device is locked|Unable to launch|Could not connect to|Failed to start remote service|was not, or could no longer be, found|Lost connection to" "$1"
}

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
    read -r background cinematic live_old < <(scenario_env "$scenario")
    [[ "$live_old" != "-" ]] || live_old=""
    run_dir="$RUN_ROOT/$scenario--run$run"
    if [[ "$RESUME" -eq 1 && -f "$run_dir/summary.json" ]] && grep -q '"xcodebuild_exit": 0' "$run_dir/status.json" 2>/dev/null; then
      echo "talk-bench: $scenario run $run/$RUNS already good, kept"
      continue
    fi
    rm -rf "$run_dir"
    mkdir -p "$run_dir"
    echo "talk-bench: $scenario run $run/$RUNS"
    exit_code=0
    attempts=0
    infra_attempts=0
    test_attempts=0
    gave_up=""
    while :; do
      attempts=$((attempts + 1))
      if ! wait_for_unlocked_device 180; then gave_up="the phone stayed locked for 180 s"; exit_code=75; break; fi
      competing="$(competing_device_users)"
      [[ -n "$competing" ]] && echo "talk-bench: WARNING another process is using the phone: $competing" >&2
      thermal_before="$(cool_down_device)"
      [[ "$REINSTALL" -eq 1 ]] && uninstall_app
      exit_code=0
      poller=""
      if [[ "$POLL_FILES" -eq 1 ]]; then poll_files "$run_dir/polled" & poller=$!; fi
      TEST_RUNNER_KRIA_TELEPROMPTER_HARDWARE_TEST=1 TEST_RUNNER_KRIA_TALK_BENCH=1 \
      TEST_RUNNER_KRIA_TALK_USETAKE_DWELL="$USE_TAKE_DWELL" TEST_RUNNER_KRIA_TALK_USETAKE_AFTER="$USE_TAKE_AFTER" \
      TEST_RUNNER_KRIA_TALK_BENCH_BACKGROUND="$background" TEST_RUNNER_KRIA_TALK_BENCH_CINEMATIC="$cinematic" TEST_RUNNER_KRIA_TALK_BENCH_LIVE_OLD="$live_old" \
      TEST_RUNNER_KRIA_TALK_BENCH_SECONDS="$SECONDS_PER_TAKE" TEST_RUNNER_KRIA_TALK_BENCH_IDLE_SECONDS="$IDLE_SECONDS" TEST_RUNNER_KRIA_TALK_BENCH_REVIEW_TIMEOUT="$REVIEW_TIMEOUT" TEST_RUNNER_KRIA_TALK_BENCH_USE_TAKE="$USE_TAKE" \
        xcodebuild test-without-building -project "$IOS_ROOT/Kria.xcodeproj" -scheme "$SCHEME" -configuration Debug \
          -destination "platform=iOS,id=$DEVICE_ID" -derivedDataPath "$DERIVED_DATA" -enableCodeCoverage NO \
          -resultBundlePath "$run_dir/result.xcresult" -only-testing "$TEST_ID" >"$run_dir/xcodebuild.log" 2>&1 || exit_code=$?
      if [[ -n "$poller" ]]; then kill "$poller" 2>/dev/null || true; wait "$poller" 2>/dev/null || true; fi
      [[ "$exit_code" -eq 0 ]] && break
      if infrastructure_failure "$run_dir/xcodebuild.log"; then
        infra_attempts=$((infra_attempts + 1))
        [[ "$infra_attempts" -le "$RETRIES" ]] || break
        echo "talk-bench:   install/launch failed (attempt $attempts), retrying in 20 s" >&2
      else
        test_attempts=$((test_attempts + 1))
        [[ "$test_attempts" -le "$TEST_RETRIES" ]] || break
        echo "talk-bench:   the test failed (attempt $attempts), rerunning" >&2
      fi
      mv "$run_dir/xcodebuild.log" "$run_dir/xcodebuild.attempt$attempts.log"
      rm -rf "$run_dir/result.xcresult"
      sleep 20
    done
    reason="$gave_up"
    grep -q "Test Case .* skipped" "$run_dir/xcodebuild.log" 2>/dev/null && reason="the test was skipped (opt-in flags or an unsupported camera)"
    # A run that never started the test leaves nothing, or the previous run's Documents/talk-bench, on the phone; pulling it
    # would report old numbers as new. A test that started and then failed wrote its own numbers, which are kept as partial
    # evidence. An install or launch failure is not a started test, whatever the log says.
    [[ "$exit_code" -ne 0 && -z "$reason" ]] && reason="xcodebuild exited $exit_code, see xcodebuild.log"
    infra_failed=0
    [[ -f "$run_dir/xcodebuild.log" ]] && infrastructure_failure "$run_dir/xcodebuild.log" && infra_failed=1
    [[ "$infra_failed" -eq 1 && "$exit_code" -ne 0 ]] && reason="install or launch failed after $attempts attempt(s), see xcodebuild.log"
    if [[ "$infra_failed" -eq 0 || "$exit_code" -eq 0 ]] && [[ -f "$run_dir/xcodebuild.log" ]] \
        && grep -q "Test Case .* started" "$run_dir/xcodebuild.log" && ! grep -q "Test Case .* skipped" "$run_dir/xcodebuild.log"; then
      pull_run "$run_dir"
    fi
    printf '{"xcodebuild_exit": %d, "reason": "%s", "attempts": %d, "transport": "%s", "thermal_before_run": "%s"}\n' "$exit_code" "$reason" "$attempts" "$(device_transport)" "${thermal_before:-unknown}" >"$run_dir/status.json"
    python3 "$REPORT_PY" analyze-take "$run_dir" >/dev/null || true
    echo "talk-bench:   xcodebuild exit $exit_code, $(ls "$run_dir" | grep -c '^take-.*\.\(mov\|mp4\)$' || true) take(s) pulled"
  done
done

finish_report "$RUNS"
