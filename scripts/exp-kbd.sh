#!/usr/bin/env bash
# Throwaway (KRI-1604): run the named UI tests once per xcodebuild call, N calls, with CI's time limits and
# no retries, while the app logs what is still animating. Prints each never-idle wait with the app's log.
# usage: exp-kbd.sh <label> <passes> <busy processes> <Class/test>...
set -uo pipefail
label="$1"; passes="$2"; busy="$3"; shift 3
cd src/apps/ios
common=(-project Kria.xcodeproj -scheme Kria -skipPackagePluginValidation -derivedDataPath .derived-data CODE_SIGNING_ALLOWED=NO -destination "platform=iOS Simulator,id=$SIM")
xcodebuild "${common[@]}" build-for-testing > "build-$label.log" 2>&1 || { grep -E "error:" "build-$label.log" | head -20; echo "BUILD FAILED ($label)"; exit 1; }
only=(); for id in "$@"; do only+=("-only-testing:KriaUITests/$id"); done
xcrun simctl spawn "$SIM" log stream --style compact --predicate 'process == "Kria" AND eventMessage CONTAINS "KBDPROBE"' > "probe-$label.log" 2>&1 &
stream=$!
pids=()
if [ "$busy" -gt 0 ]; then for i in $(seq "$busy"); do yes > /dev/null & pids+=($!); done; fi
: > "$label.log"
for pass in $(seq "$passes"); do
  echo "=== PASS $pass $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$label.log"
  xcodebuild "${common[@]}" -parallel-testing-enabled NO -test-timeouts-enabled YES -default-test-execution-time-allowance 180 -maximum-test-execution-time-allowance 300 "${only[@]}" test-without-building >> "$label.log" 2>&1
  echo "=== PASS $pass exit $?" >> "$label.log"
done
if [ "${#pids[@]}" -gt 0 ]; then kill "${pids[@]}" 2>/dev/null || true; fi
sleep 3; kill "$stream" 2>/dev/null || true
/usr/bin/python3 ../../../scripts/exp-kbd-report.py "$label" "$label.log" "probe-$label.log"
exit 0
