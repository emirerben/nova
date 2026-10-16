#!/usr/bin/env bash
# Performance benchmark for the Kria iOS app: launch, screen-open, scroll, gallery.
#
#   scripts/ios/bench.sh --label base                          # build, run everything, save the build
#   scripts/ios/bench.sh --label fix --vs base                 # build, then run fix AGAINST base, interleaved
#   scripts/ios/bench.sh --label fix --vs base --only testLaunchToTranscript --rounds 4 --iterations 12
#   scripts/ios/bench.sh --dest device --device-id <udid> --label base
#   scripts/ios/bench.sh --compare a.json b.json
#
# WHY INTERLEAVED. Two runs of the SAME binary minutes apart differ by 10-25% on this machine (thermal state,
# background load, simulator caches), more than most fixes gain. A fix measured against a baseline taken earlier
# can therefore look like a win or a loss that is not there. `--vs` runs both builds in alternating rounds
# (A B, B A, A B, ...) in one session and pools each side's samples, so drift hits both equally and shows up in
# the spread instead of in the verdict. Deterministic counts (requests, rebuilds, network fetches) do not drift
# at all and are the first evidence to reach for.
#
# BUILDS. Every build is optimized (Debug config for the fixtures, SWIFT_OPTIMIZATION_LEVEL=-O, whole-module)
# with coverage off, and is saved under $KRIA_BENCH_BUILDS/<label>-<dest> (default ~/.cache/kria-bench/builds) as
# a relocatable test bundle, so a baseline survives later rebuilds and other worktrees.
#
# Results: test-results/ios/bench/<label>-<sha>-<dest>.json (+ .log). Compare with `--compare`, or let `--vs` do it.
# Simulator: KRIA_SIMULATOR_ID, else the one named "Kria Perf" (xcrun simctl create "Kria Perf" "iPhone 17 Pro"),
# else the newest iPhone. A dedicated simulator keeps other sessions' booted simulators from adding noise.
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
IOS_ROOT="$REPO_ROOT/src/apps/ios"
DERIVED_DATA="$IOS_ROOT/.derived-data-perf"
OUT_DIR="$REPO_ROOT/test-results/ios/bench"
BUILDS="${KRIA_BENCH_BUILDS:-$HOME/.cache/kria-bench/builds}"
REPORT="$REPO_ROOT/scripts/ios/bench_report.py"

DEST="sim"
ITERATIONS=10
ROUNDS=4
ONLY=""
LABEL=""
VS=""
DEVICE_ID="${KRIA_DEVICE_ID:-}"
SKIP_BUILD=0

xcbeautify_or_cat() { if command -v xcbeautify >/dev/null 2>&1; then xcbeautify --quiet; else cat; fi; }
usage() { sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

if [[ "${1:-}" == "--compare" ]]; then
  shift
  [[ $# -eq 2 ]] || { echo "--compare needs two result files" >&2; exit 2; }
  exec python3 "$REPORT" compare "$1" "$2"
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dest) DEST="$2"; shift 2 ;;
    --iterations) ITERATIONS="$2"; shift 2 ;;
    --rounds) ROUNDS="$2"; shift 2 ;;
    --only) ONLY="$2"; shift 2 ;;
    --label) LABEL="$2"; shift 2 ;;
    --vs) VS="$2"; shift 2 ;;
    --device-id) DEVICE_ID="$2"; shift 2 ;;
    --skip-build) SKIP_BUILD=1; shift ;;
    -h|--help) usage 0 ;;
    *) echo "Unknown argument: $1" >&2; usage 2 ;;
  esac
done

[[ "$DEST" == "sim" || "$DEST" == "device" ]] || { echo "--dest must be sim or device" >&2; exit 2; }
[[ -n "$LABEL" ]] || { echo "--label is required (e.g. base, fix-token-refresh)" >&2; exit 2; }

select_simulator() {
  xcrun simctl list devices available -j | /usr/bin/python3 -c '
import json, os, sys
requested = os.environ.get("KRIA_SIMULATOR_ID", "")
payload = json.load(sys.stdin)
candidates = []
for runtime, devices in payload.get("devices", {}).items():
    if "iOS" not in runtime:
        continue
    for device in devices:
        if device.get("isAvailable") and device.get("name", "").startswith("iPhone"):
            candidates.append((runtime, device))
if requested:
    for _, device in candidates:
        if device["udid"] == requested:
            print(device["udid"]); raise SystemExit(0)
    raise SystemExit("Requested simulator is unavailable: " + requested)
for _, device in candidates:
    if device["name"] == "Kria Perf":
        print(device["udid"]); raise SystemExit(0)
candidates.sort(key=lambda item: item[0])
print(candidates[-1][1]["udid"])
'
}

if [[ "$DEST" == "sim" ]]; then
  TARGET_ID="$(select_simulator)"
  DESTINATION="platform=iOS Simulator,id=$TARGET_ID"
  xcrun simctl bootstatus "$TARGET_ID" -b >/dev/null 2>&1 || true
  BUILD_SETTINGS=(CODE_SIGNING_ALLOWED=NO)
else
  [[ -n "$DEVICE_ID" ]] || { echo "--dest device needs --device-id <udid> (or KRIA_DEVICE_ID); see: xcrun devicectl list devices" >&2; exit 2; }
  TARGET_ID="$DEVICE_ID"
  DESTINATION="platform=iOS,id=$TARGET_ID"
  BUILD_SETTINGS=(-allowProvisioningUpdates)
fi

SHA="$(git -C "$REPO_ROOT" rev-parse --short HEAD)"
if [[ -n "$(git -C "$REPO_ROOT" status --porcelain -- src/apps/ios)" ]]; then SHA="$SHA-dirty"; fi
mkdir -p "$OUT_DIR" "$BUILDS"
SAVED="$BUILDS/$LABEL-$DEST"

# --- build once, then save the products as a relocatable test bundle ---------------------------------------------
if [[ "$SKIP_BUILD" -eq 0 ]]; then
  bash "$REPO_ROOT/scripts/ios/generate-project.sh"
  xcodebuild build-for-testing \
    -project "$IOS_ROOT/Kria.xcodeproj" -scheme "Kria Perf" -configuration Debug \
    -destination "$DESTINATION" -derivedDataPath "$DERIVED_DATA" \
    -enableCodeCoverage NO SWIFT_OPTIMIZATION_LEVEL=-O SWIFT_COMPILATION_MODE=wholemodule \
    "${BUILD_SETTINGS[@]}" | xcbeautify_or_cat
  rm -rf "$SAVED"
  mkdir -p "$SAVED"
  cp -R "$DERIVED_DATA/Build/Products/." "$SAVED/"
  echo "bench: saved build to $SAVED"
fi
[[ -d "$SAVED" ]] || { echo "No saved build for '$LABEL' on $DEST ($SAVED); run without --skip-build first" >&2; exit 2; }

xctestrun_in() {
  local found
  found="$(ls "$1"/*.xctestrun 2>/dev/null | head -1)"
  [[ -n "$found" ]] || { echo "No .xctestrun in $1" >&2; exit 2; }
  echo "$found"
}

ONLY_ARGS=()
if [[ -n "$ONLY" ]]; then
  IFS=',' read -ra TESTS <<<"$ONLY"
  for name in "${TESTS[@]}"; do ONLY_ARGS+=(-only-testing "KriaPerfUITests/PerfUITests/$name"); done
else
  ONLY_ARGS+=(-only-testing "KriaPerfUITests/PerfUITests")
fi

# run_pass <saved-build-dir> <log> <iterations> : append one xcodebuild test pass to <log>
run_pass() {
  local build="$1" log="$2" iterations="$3" bundle
  bundle="$(mktemp -d "$OUT_DIR/pass.XXXXXX")"
  TEST_RUNNER_KRIA_PERF_ITERATIONS="$iterations" xcodebuild test-without-building \
    -xctestrun "$(xctestrun_in "$build")" -destination "$DESTINATION" \
    -resultBundlePath "$bundle/result.xcresult" -enableCodeCoverage NO \
    "${ONLY_ARGS[@]}" 2>&1 | tee -a "$log" | xcbeautify_or_cat || echo "bench: a pass had failures; reporting what was measured" >&2
  rm -rf "$bundle"
}

report() { # report <label> <sha> <out> <log>...
  local label="$1" sha="$2" out="$3"; shift 3
  local logs=(); for l in "$@"; do logs+=(--log "$l"); done
  python3 "$REPORT" report "${logs[@]}" --label "$label" --sha "$sha" --dest "$DEST" --iterations "$ITERATIONS" --out "$out"
}

echo "bench: label=$LABEL vs=${VS:-none} dest=$DEST target=$TARGET_ID iterations=$ITERATIONS sha=$SHA"

if [[ -z "$VS" ]]; then
  LOG="$OUT_DIR/$LABEL-$SHA-$DEST.log"
  rm -f "$LOG"
  run_pass "$SAVED" "$LOG" "$ITERATIONS"
  report "$LABEL" "$SHA" "$OUT_DIR/$LABEL-$SHA-$DEST.json" "$LOG"
  exit 0
fi

# --- interleaved A/B ---------------------------------------------------------------------------------------------
OTHER="$BUILDS/$VS-$DEST"
[[ -d "$OTHER" ]] || { echo "No saved build for --vs '$VS' on $DEST ($OTHER)" >&2; exit 2; }
PER_ROUND=$(( (ITERATIONS + ROUNDS - 1) / ROUNDS ))
LOG_A="$OUT_DIR/$LABEL-$SHA-$DEST.log"
LOG_B="$OUT_DIR/$VS-vs-$LABEL-$DEST.log"
rm -f "$LOG_A" "$LOG_B"
for round in $(seq 1 "$ROUNDS"); do
  # Alternate who goes first so a slow start or a warming machine does not favour one side.
  if (( round % 2 == 1 )); then
    echo "bench: round $round/$ROUNDS: $LABEL then $VS ($PER_ROUND iterations each)"
    run_pass "$SAVED" "$LOG_A" "$PER_ROUND"; run_pass "$OTHER" "$LOG_B" "$PER_ROUND"
  else
    echo "bench: round $round/$ROUNDS: $VS then $LABEL ($PER_ROUND iterations each)"
    run_pass "$OTHER" "$LOG_B" "$PER_ROUND"; run_pass "$SAVED" "$LOG_A" "$PER_ROUND"
  fi
done
JSON_A="$OUT_DIR/$LABEL-$SHA-$DEST.json"
JSON_B="$OUT_DIR/$VS-vs-$LABEL-$DEST.json"
report "$LABEL" "$SHA" "$JSON_A" "$LOG_A" >/dev/null
report "$VS" "baseline" "$JSON_B" "$LOG_B" >/dev/null
echo
python3 "$REPORT" compare "$JSON_B" "$JSON_A"
