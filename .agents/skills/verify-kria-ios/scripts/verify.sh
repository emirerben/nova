#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
EVIDENCE="${KRIA_VERIFY_EVIDENCE:-$ROOT/.dev/verify-kria-ios}"
BUILD="$EVIDENCE/ios-build"
STATE="$EVIDENCE/simulator"
cd "$ROOT"
mkdir -p "$EVIDENCE"

doctor() {
  test -s "$STATE" || { echo 'No owned simulator. Run launch.' >&2; return 1; }
  local simulator
  simulator="$(cat "$STATE")"
  xcrun simctl list devices -j | python3 -c '
import json, sys
identifier = sys.argv[1]
assert any(d["udid"] == identifier and d["state"] == "Booted"
           for ds in json.load(sys.stdin)["devices"].values() for d in ds), "simulator is not booted"
' "$simulator"
  test -s "$EVIDENCE/input-fingerprint"
  test "$(cat "$EVIDENCE/input-fingerprint")" = "$(python3 scripts/ios/cache-inputs.py fingerprint)"
  python3 -c 'from pathlib import Path; import sys; assert list(Path(sys.argv[1]).glob("Build/Products/*.xctestrun")), "test runner is missing"' "$BUILD"
  echo "Doctor passed for $simulator"
}

case "${1:-}" in
  launch)
    if [[ ! -f "$STATE" ]]; then
      runtime="$(xcrun simctl list runtimes -j | python3 -c 'import json,sys; r=[r for r in json.load(sys.stdin)["runtimes"] if r.get("isAvailable") and r["identifier"].startswith("com.apple.CoreSimulator.SimRuntime.iOS-")]; assert r, "no iOS runtime"; print(sorted(r,key=lambda r: tuple(map(int,r["version"].split("."))))[-1]["identifier"])')"
      xcrun simctl create 'Kria verification' com.apple.CoreSimulator.SimDeviceType.iPhone-17 "$runtime" > "$STATE"
    fi
    simulator="$(cat "$STATE")"
    xcrun simctl bootstatus "$simulator" -b > "$EVIDENCE/boot.log" 2>&1
    bash scripts/ios/generate-project.sh > "$EVIDENCE/project.log" 2>&1
    xcodebuild -project src/apps/ios/Kria.xcodeproj -scheme Kria -derivedDataPath "$BUILD" \
      -destination "platform=iOS Simulator,id=$simulator" -skipPackagePluginValidation \
      CODE_SIGNING_ALLOWED=NO -quiet build-for-testing > "$EVIDENCE/build.log" 2>&1
    python3 scripts/ios/cache-inputs.py fingerprint > "$EVIDENCE/input-fingerprint"
    git rev-parse HEAD > "$EVIDENCE/build-head"
    doctor
    ;;
  doctor) doctor ;;
  drive)
    doctor
    case "${2:-}" in
      chat) selection='KriaUITests/CreationUITests/testCreationWithAttachedFootageReachesConfirmationAndReadyForBothRuntimes' ;;
      media) selection='KriaUITests/CreationUITests/testPickerShowsAVisualsPhotoWithItsThumbnailAndAPhotoName' ;;
      editor) selection='KriaUITests/NativeEditorInspectorUITests/testApprovingACaptionLineCommitsItAndEnablesSaveOnceThePanelCloses' ;;
      delivery) selection='KriaUITests/DeviceRenderUITests/testPhoneRenderReachesReadyWithPlaySaveAndShare' ;;
      slides) selection='KriaUITests/SlidePostUITests/testDrawOnASlideLeavesInkThatSurvivesSaveAndSlideSwitching' ;;
      *) echo 'Choose chat, media, editor, delivery or slides.' >&2; exit 2 ;;
    esac
    attempt="$(mktemp -d "$EVIDENCE/${2}.XXXXXX")"
    simulator="$(cat "$STATE")"
    drive_status=0
    mkdir -p "$attempt/screenshots"
    TEST_RUNNER_KRIA_SHOT_DIR="$attempt/screenshots" xcodebuild \
      -project src/apps/ios/Kria.xcodeproj -scheme Kria -derivedDataPath "$BUILD" \
      -destination "platform=iOS Simulator,id=$simulator" -skipPackagePluginValidation \
      -parallel-testing-enabled NO -only-testing:"$selection" -resultBundlePath "$attempt/result.xcresult" \
      CODE_SIGNING_ALLOWED=NO test-without-building > "$attempt/drive.log" 2>&1 || drive_status=$?
    if [[ -d "$attempt/result.xcresult" ]]; then
      xcrun xcresulttool export attachments --path "$attempt/result.xcresult" \
        --output-path "$attempt/attachments" > "$attempt/attachments.log" 2>&1
      xcrun xcresulttool get test-results summary --path "$attempt/result.xcresult" > "$attempt/summary.json"
    fi
    echo "Evidence retained at $attempt"
    [[ "$drive_status" -eq 0 ]] || exit "$drive_status"
    python3 -c 'import json,sys; r=json.load(open(sys.argv[1])); assert r["passedTests"] >= 1 and r["failedTests"] == 0 and r["skippedTests"] == 0, "selected tests did not all pass"' "$attempt/summary.json"
    ;;
  cleanup)
    if [[ -s "$STATE" ]]; then
      simulator="$(cat "$STATE")"
      xcrun simctl shutdown "$simulator" > "$EVIDENCE/cleanup.log" 2>&1 || true
      xcrun simctl delete "$simulator" >> "$EVIDENCE/cleanup.log" 2>&1
      mv "$STATE" "$EVIDENCE/retired-simulator-$(date -u +%Y%m%dT%H%M%SZ)"
    fi
    echo "Evidence remains at $EVIDENCE"
    ;;
  *) echo 'Use launch, doctor, drive <feature>, or cleanup.' >&2; exit 2 ;;
esac
