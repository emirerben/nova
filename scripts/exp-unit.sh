#!/usr/bin/env bash
# Throwaway (KRI-1605, KRI-1607): run unit test classes with no retries, idle or with busy processes.
# usage: exp-unit.sh <label> <iterations> <busy processes> <only-testing id>...
set -uo pipefail
label="$1"; iterations="$2"; busy="$3"; shift 3
cd src/apps/ios
common=(-project Kria.xcodeproj -scheme Kria -skipPackagePluginValidation -derivedDataPath .derived-data CODE_SIGNING_ALLOWED=NO -destination "platform=iOS Simulator,id=$SIM")
if [ ! -f ".built-$(git rev-parse HEAD)-$(git diff HEAD | shasum | cut -c1-12)" ]; then
  xcodebuild "${common[@]}" build-for-testing > "$label-build.log" 2>&1 || { echo "[$label] BUILD FAILED"; grep -E "error:" "$label-build.log" | head -20; exit 1; }
  rm -f .built-*; touch ".built-$(git rev-parse HEAD)-$(git diff HEAD | shasum | cut -c1-12)"
fi
only=(); for id in "$@"; do only+=("-only-testing:KriaTests/$id"); done
[ "${#only[@]}" -eq 0 ] && only=(-only-testing:KriaTests)
repeat=(); [ "$iterations" -gt 1 ] && repeat=(-test-iterations "$iterations")
pids=()
if [ "$busy" -gt 0 ]; then for i in $(seq "$busy"); do yes > /dev/null & pids+=($!); done; fi
status=0
xcodebuild "${common[@]}" -parallel-testing-enabled NO -test-timeouts-enabled YES -default-test-execution-time-allowance 120 -maximum-test-execution-time-allowance 300 "${only[@]}" "${repeat[@]}" test-without-building > "$label.log" 2>&1 || status=$?
if [ "${#pids[@]}" -gt 0 ]; then kill "${pids[@]}" 2>/dev/null || true; fi
[ "$status" -eq 64 ] && tail -5 "$label.log"
echo "[$label] xcodebuild exit $status; test runs passed $(grep -c "\]' passed" "$label.log" || true), failed $(grep -c "\]' failed" "$label.log" || true)"
echo "[$label] failed tests:"; grep -o "Test Case '-\[KriaTests\.[A-Za-z]* [A-Za-z_]*\]' failed" "$label.log" | sed -E "s/^.*\[KriaTests\.([A-Za-z]+ [A-Za-z_]+)\]' failed/\1/" | sort | uniq -c || true
echo "[$label] failure messages:"; grep -o "error: -\[.*" "$label.log" | sed -E 's/^error: -\[KriaTests\.//' | cut -c1-260 | sort | uniq -c | head -20 || true
echo "[$label] time-limit hits:"; grep -c "exceeded execution time allowance" "$label.log" || true
echo "[$label] requests from an earlier test:"; grep -E "Requests from an earlier test|^  [0-9]+ during " "$label.log" | head -40 || true
exit 0
