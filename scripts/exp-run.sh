#!/usr/bin/env bash
# Throwaway (KRI-1617): build, run the named UI tests N times each with no retries, print what happened.
# usage: exp-run.sh <label> <iterations> <busy processes> <Class/test>...
set -uo pipefail
label="$1"; iterations="$2"; busy="$3"; shift 3
cd src/apps/ios
common=(-project Kria.xcodeproj -scheme Kria -skipPackagePluginValidation -derivedDataPath .derived-data CODE_SIGNING_ALLOWED=NO -destination "platform=iOS Simulator,id=$SIM")
xcodebuild "${common[@]}" build-for-testing > "build-$label.log" 2>&1 || { grep -E "error:" "build-$label.log" | head -20; echo "BUILD FAILED ($label)"; exit 1; }
only=(); for id in "$@"; do only+=("-only-testing:KriaUITests/$id"); done
pids=()
if [ "$busy" -gt 0 ]; then for i in $(seq "$busy"); do yes > /dev/null & pids+=($!); done; fi
status=0
xcodebuild "${common[@]}" -parallel-testing-enabled NO -test-timeouts-enabled YES -default-test-execution-time-allowance 300 -maximum-test-execution-time-allowance 400 "${only[@]}" -test-iterations "$iterations" test-without-building > "$label.log" 2>&1 || status=$?
if [ "${#pids[@]}" -gt 0 ]; then kill "${pids[@]}" 2>/dev/null || true; fi
echo "[$label] xcodebuild exit $status; test runs passed $(grep -c "\]' passed" "$label.log" || true), failed $(grep -c "\]' failed" "$label.log" || true)"
echo "[$label] failure messages:"; grep -o "error: -\[.*" "$label.log" | sed -E 's/^error: -\[KriaUITests\.[A-Za-z]+ //' | cut -c1-250 | sed -E 's/[0-9]+\.[0-9]+/N/g' | sort | uniq -c | head -14 || true
echo "[$label] time-limit hits:"; grep -c "exceeded execution time allowance" "$label.log" || true
echo "[$label] Save tap to Saved:"; grep -o "SAVE-ELAPSED .*" "$label.log" | cut -c1-160 || true
exit 0
