#!/usr/bin/env bash
# Throwaway (KRI-1607): run one unit test class once per xcodebuild call, many times, so each pass crosses from
# every test into the next one exactly as the suite does. usage: exp-class-loop.sh <label> <passes> <busy> <Class>
set -uo pipefail
label="$1"; passes="$2"; busy="$3"; class="$4"
cd src/apps/ios
common=(-project Kria.xcodeproj -scheme Kria -skipPackagePluginValidation -derivedDataPath .derived-data CODE_SIGNING_ALLOWED=NO -destination "platform=iOS Simulator,id=$SIM")
xcodebuild "${common[@]}" build-for-testing > "$label-build.log" 2>&1 || { echo "[$label] BUILD FAILED"; grep -E "error:" "$label-build.log" | head -20; exit 1; }
pids=()
if [ "$busy" -gt 0 ]; then for i in $(seq "$busy"); do yes > /dev/null & pids+=($!); done; fi
red=0; : > "$label.log"
for pass in $(seq "$passes"); do
  status=0
  xcodebuild "${common[@]}" -parallel-testing-enabled NO -test-timeouts-enabled YES -default-test-execution-time-allowance 120 -maximum-test-execution-time-allowance 300 "-only-testing:KriaTests/$class" test-without-building > "$label-pass.log" 2>&1 || status=$?
  [ "$status" -ne 0 ] && red=$((red + 1))
  cat "$label-pass.log" >> "$label.log"
done
if [ "${#pids[@]}" -gt 0 ]; then kill "${pids[@]}" 2>/dev/null || true; fi
echo "[$label] passes $passes, passes not green $red; test runs passed $(grep -c "\]' passed" "$label.log" || true), failed $(grep -c "\]' failed" "$label.log" || true)"
echo "[$label] failed tests:"; grep -o "Test Case '-\[KriaTests\.[A-Za-z]* [A-Za-z_]*\]' failed" "$label.log" | sed -E "s/^.*\[KriaTests\.([A-Za-z]+ [A-Za-z_]+)\]' failed/\1/" | sort | uniq -c || true
echo "[$label] failure messages:"; grep -o "error: -\[.*" "$label.log" | sed -E 's/^error: -\[KriaTests\.//' | cut -c1-260 | sort | uniq -c | head -20 || true
echo "[$label] time-limit hits:"; grep -c "exceeded execution time allowance" "$label.log" || true
echo "[$label] requests from an earlier test, by sender and receiver:"; grep -E "^  [0-9]+ from " "$label.log" | sed -E 's/^  ([0-9]+) (from .*)$/\1\t\2/' | awk -F'\t' '{ n[$2] += $1 } END { for (k in n) print "  " n[k] " " k }' || true
exit 0
