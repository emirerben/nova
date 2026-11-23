#!/usr/bin/env bash
# Throwaway (KRI-1605): repeat one unit test class with no retries, idle or with busy processes.
set -uo pipefail
label="$1"; iterations="$2"; busy="$3"; class="$4"
cd src/apps/ios
common=(-project Kria.xcodeproj -scheme Kria -skipPackagePluginValidation -derivedDataPath .derived-data CODE_SIGNING_ALLOWED=NO -destination "platform=iOS Simulator,id=$SIM")
pids=()
if [ "$busy" -gt 0 ]; then for i in $(seq "$busy"); do yes > /dev/null & pids+=($!); done; fi
status=0
xcodebuild "${common[@]}" -parallel-testing-enabled NO -only-testing:"KriaTests/$class" -test-iterations "$iterations" test-without-building > "$label.log" 2>&1 || status=$?
if [ "${#pids[@]}" -gt 0 ]; then kill "${pids[@]}" 2>/dev/null || true; fi
echo "[$label] xcodebuild exit $status; test runs passed $(grep -c "\]' passed" "$label.log" || true), failed $(grep -c "\]' failed" "$label.log" || true)"
echo "[$label] failed tests:"; grep -o "Test Case '-\[KriaTests\.[A-Za-z]* [A-Za-z]*\]' failed" "$label.log" | sed -E "s/^.* ([A-Za-z]+)\]' failed/\1/" | sort | uniq -c || true
echo "[$label] failure messages:"; grep -o "error: -\[.*" "$label.log" | sed -E 's/^error: -\[KriaTests\.[A-Za-z]+ //' | cut -c1-250 | sort | uniq -c | head -12 || true
echo "[$label] seconds of failed runs:"; grep -o "\]' failed ([0-9.]* seconds)" "$label.log" | sed -E "s/^.*\(([0-9.]+) seconds\)/\1/" | tr '\n' ' '; echo
exit 0
