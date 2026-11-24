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
xcodebuild "${common[@]}" -parallel-testing-enabled NO -test-timeouts-enabled YES -default-test-execution-time-allowance 120 -maximum-test-execution-time-allowance 300 "${only[@]}" ${repeat[@]+"${repeat[@]}"} test-without-building > "$label.log" 2>&1 || status=$?
if [ "${#pids[@]}" -gt 0 ]; then kill "${pids[@]}" 2>/dev/null || true; fi
[ "$status" -eq 64 ] && tail -5 "$label.log"
echo "[$label] xcodebuild exit $status; test runs passed $(grep -c "\]' passed" "$label.log" || true), failed $(grep -c "\]' failed" "$label.log" || true)"
echo "[$label] failed tests:"; grep -o "Test Case '-\[KriaTests\.[A-Za-z]* [A-Za-z_]*\]' failed" "$label.log" | sed -E "s/^.*\[KriaTests\.([A-Za-z]+ [A-Za-z_]+)\]' failed/\1/" | sort | uniq -c || true
echo "[$label] failure messages:"; grep -o "error: -\[.*" "$label.log" | sed -E 's/^error: -\[KriaTests\.//' | cut -c1-260 | sort | uniq -c | head -20 || true
echo "[$label] time-limit hits:"; grep -c "exceeded execution time allowance" "$label.log" || true
echo "[$label] around each time-limit hit:"; grep -n -B4 -A6 -E "exceeded execution time allowance|Restarting after|Test crashed|unexpected exit" "$label.log" | cut -c1-400 | head -80 || true
echo "[$label] tests that started and never finished:"; awk '/Test Case .* started/ { name = $0; sub(/^.*Test Case ./, "", name); sub(/. started.*$/, "", name); open[name] = 1 } /Test Case .* (passed|failed|skipped)/ { name = $0; sub(/^.*Test Case ./, "", name); sub(/. (passed|failed|skipped).*$/, "", name); delete open[name] } END { for (n in open) print "  " n }' "$label.log"
echo "[$label] slowest tests:"; grep -oE "Test Case '-\[[A-Za-z.]+ [A-Za-z_0-9]+\]' (passed|failed) \([0-9.]+ seconds\)" "$label.log" | sed -E "s/^.*KriaTests\.([A-Za-z]+ [A-Za-z_0-9]+)\]' (passed|failed) \(([0-9.]+) seconds\)/\3 \2 \1/" | sort -rn | head -8
echo "[$label] requests from an earlier test:"; grep -E "Requests from an earlier test|^  [0-9]+ during " "$label.log" | head -40 || true
exit 0
