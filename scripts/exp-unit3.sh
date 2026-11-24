#!/usr/bin/env bash
# Throwaway (KRI-1617): build, run the three save-during-load unit tests N times each, print results per test.
# usage: exp-unit3.sh <label> <iterations>
set -uo pipefail
label="$1"; iterations="$2"
cd src/apps/ios
common=(-project Kria.xcodeproj -scheme Kria -skipPackagePluginValidation -derivedDataPath .derived-data CODE_SIGNING_ALLOWED=NO -destination "platform=iOS Simulator,id=$SIM")
xcodebuild "${common[@]}" build-for-testing > "build-$label.log" 2>&1 || { grep -E "error:" "build-$label.log" | head -20; echo "BUILD FAILED ($label)"; exit 1; }
only=()
for name in testSaveTappedWhileTheEditorIsStillPreparingRunsOnceItIsReady testASaveKeptForALoadWhoseSourcesFailStillRuns testAnEditMadeWhileTheEditorPreparesIsNotSavedUnasked; do only+=("-only-testing:KriaTests/NativeEditorSessionTests/$name"); done
status=0
xcodebuild "${common[@]}" -parallel-testing-enabled NO -test-timeouts-enabled YES -default-test-execution-time-allowance 60 -maximum-test-execution-time-allowance 120 "${only[@]}" -test-iterations "$iterations" test-without-building > "$label.log" 2>&1 || status=$?
echo "[$label] xcodebuild exit $status; test runs passed $(grep -c "\]' passed" "$label.log" || true), failed $(grep -c "\]' failed" "$label.log" || true)"
echo "[$label] by test:"; grep -o "Test Case '-\[KriaTests\.NativeEditorSessionTests [A-Za-z]*\]' [a-z]*" "$label.log" | sed -E "s/^.*Tests ([A-Za-z]+)\]' ([a-z]+)$/\1 \2/" | sort | uniq -c || true
echo "[$label] failure messages:"; grep -o "error: -\[.*" "$label.log" | sed -E 's/^error: -\[KriaTests\.NativeEditorSessionTests //' | cut -c1-260 | sort | uniq -c | head -14 || true
echo "[$label] time-limit hits:"; grep -c "exceeded execution time allowance" "$label.log" || true
exit 0
