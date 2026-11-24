#!/usr/bin/env bash
# Throwaway (KRI-1604): run the named UI tests once per xcodebuild call, N calls, with CI's time limits and
# no retries, while the app logs what is still animating and how much work its text box does. When the app
# says the box has held the keyboard for 20 s and 40 s ("KBDPROBE stall"), the app's process is sampled.
# usage: exp-kbd.sh <label> <passes> <busy processes> <Class/test>...
set -uo pipefail
label="$1"; passes="$2"; busy="$3"; shift 3
cd src/apps/ios
common=(-project Kria.xcodeproj -scheme Kria -skipPackagePluginValidation -derivedDataPath .derived-data CODE_SIGNING_ALLOWED=NO -destination "platform=iOS Simulator,id=$SIM")
xcodebuild "${common[@]}" build-for-testing > "build-$label.log" 2>&1 || { grep -E "error:" "build-$label.log" | head -20; echo "BUILD FAILED ($label)"; exit 1; }
only=(); for id in "$@"; do only+=("-only-testing:KriaUITests/$id"); done
xcrun simctl spawn "$SIM" log stream --style compact --predicate 'process == "Kria" AND eventMessage CONTAINS "KBDPROBE"' > "probe-$label.log" 2>&1 &
stream=$!
# Sample the app each time it reports a stall. Each stall line is acted on once, keyed by its timestamp.
: > "stalls-$label.txt"
(
  taken=0
  while :; do
    sleep 6
    xcrun simctl spawn "$SIM" log show --last 1m --style compact --predicate 'process == "Kria" AND eventMessage CONTAINS "KBDPROBE stall"' 2>/dev/null | grep "KBDPROBE stall" | while read -r day clock rest; do
      grep -q "^$day $clock\$" "stalls-$label.txt" && continue
      echo "$day $clock" >> "stalls-$label.txt"
      number=$(wc -l < "stalls-$label.txt" | tr -d ' ')
      pid=$(pgrep -x Kria | head -1)
      if [ -n "$pid" ]; then sample "$pid" 3 -file "sample-$label-$number.txt" > /dev/null 2>&1; fi
      echo "[$label] stall at $clock: sampled app pid ${pid:-none} as sample-$label-$number.txt ($(date -u +%H:%M:%S))"
    done
  done
) &
watcher=$!
pids=()
if [ "$busy" -gt 0 ]; then for i in $(seq "$busy"); do yes > /dev/null & pids+=($!); done; fi
: > "$label.log"
for pass in $(seq "$passes"); do
  echo "=== PASS $pass $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$label.log"
  began=$(date +%s)
  xcodebuild "${common[@]}" -parallel-testing-enabled NO -test-timeouts-enabled YES -default-test-execution-time-allowance 180 -maximum-test-execution-time-allowance 300 "${only[@]}" test-without-building > "$label-pass.log" 2>&1
  status=$?
  cat "$label-pass.log" >> "$label.log"
  echo "=== PASS $pass exit $status" >> "$label.log"
  echo "[$label] pass $pass exit $status in $(( $(date +%s) - began ))s; passed $(grep -c "\]' passed" "$label-pass.log" || true), failed $(grep -c "\]' failed" "$label-pass.log" || true), 60 s waits $(grep -c "App animations complete notification not received" "$label-pass.log" || true)"
done
if [ "${#pids[@]}" -gt 0 ]; then kill "${pids[@]}" 2>/dev/null || true; fi
sleep 8; kill "$watcher" 2>/dev/null || true; kill "$stream" 2>/dev/null || true
/usr/bin/python3 ../../../scripts/exp-kbd-report.py "$label" "$label.log" "probe-$label.log"
exit 0
