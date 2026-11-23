#!/usr/bin/env bash
# Throwaway (KRI-1604): build, run the named UI tests N times each with no retries, print what happened.
# usage: exp-run.sh <label> <iterations> <busy processes> <Class/test>...
set -uo pipefail
label="$1"; iterations="$2"; busy="$3"; shift 3
cd src/apps/ios
common=(-project Kria.xcodeproj -scheme Kria -skipPackagePluginValidation -derivedDataPath .derived-data CODE_SIGNING_ALLOWED=NO -destination "platform=iOS Simulator,id=$SIM")
xcodebuild "${common[@]}" build-for-testing > "build-$label.log" 2>&1 || { grep -E "error:" "build-$label.log" | head -20; tail -20 "build-$label.log"; echo "BUILD FAILED ($label)"; exit 0; }
only=()
for id in "$@"; do only+=("-only-testing:KriaUITests/$id"); done
pids=()
if [ "$busy" -gt 0 ]; then for i in $(seq "$busy"); do yes > /dev/null & pids+=($!); done; fi
status=0
xcodebuild "${common[@]}" -parallel-testing-enabled NO \
  -test-timeouts-enabled YES -default-test-execution-time-allowance 180 -maximum-test-execution-time-allowance 300 \
  "${only[@]}" -test-iterations "$iterations" test-without-building > "$label.log" 2>&1 || status=$?
if [ "${#pids[@]}" -gt 0 ]; then kill "${pids[@]}" 2>/dev/null || true; fi
echo "[$label] xcodebuild exit $status"
for id in "$@"; do
  t="${id#*/}"
  echo "[$label] $t: passed $(grep -c "$t\]' passed" "$label.log" || true), failed $(grep -c "$t\]' failed" "$label.log" || true)"
  echo "[$label] $t seconds: $(grep -o "$t\]' [a-z]* ([0-9.]* seconds)" "$label.log" | sed -E "s/^.*\(([0-9.]+) seconds\)/\1/" | tr '\n' ' ')"
done
echo "[$label] failure messages:"; grep -o "error: -\[.*" "$label.log" | sed -E 's/^error: -\[KriaUITests\.[A-Za-z]+ //' | cut -c1-250 | sed -E 's/[0-9]+\.[0-9]+/N/g' | sort | uniq -c | head -14 || true
echo "[$label] song clocks:"; grep -o "SONG-[A-Z]* .*" "$label.log" | cut -c1-260 || true
echo "[$label] format card ready at first look:"; grep -o "FORMAT-READY .*" "$label.log" | sort | uniq -c || true
echo "[$label] seconds from the delete's confirm tap to the Undo tap:"
awk '
  /testUndoTappedWhileTheEventDeleteIsInFlightKeepsItsIdeas\]. started/ { on = 1; want = ""; confirm = -1 }
  on && /Tap "camera-roll-delete-event-confirm"/ { want = "confirm" }
  on && /Tap "camera-roll-undo"/ { want = "undo" }
  on && /Synthesize event/ && want != "" {
    t = $0; sub(/^.*t = +/, "", t); sub(/s .*$/, "", t)
    if (want == "confirm") confirm = t; else if (confirm >= 0) printf "%.2f ", t - confirm
    want = ""
  }
  /testUndoTappedWhileTheEventDeleteIsInFlightKeepsItsIdeas\]. (passed|failed)/ { on = 0 }
  END { print "" }
' "$label.log"
exit 0
