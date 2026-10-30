# Gameday menu driver validation

Base: `297d20f880f93ea4b3d06bcfb31180236e3fe4f3` (PR1931), 2026-10-29.

Hosted creation job123735737625 failed before tapping Video, at the helper's stable/enabled/hittable gate. The actual screenshot shows the complete Video row, no unavailable reason, and an editor fixture in ready/save-idle state. Its AX dump exposes a 4000-point scrim and nested add-button overlay. This suggests an AX hit-test false negative; the precise SDK cause is not established.

The menu-only driver now requires existence, finite unchanged bounds on two polls, enabled state and full containment within the app window, then performs one real center tap. The plus button retains its original hittability check. No retry, timeout increase, fixture or product change persists. Fifth-clip insertion/current preview, Undo/Redo, save, process relaunch, retained title and Photos export assertions remain unchanged. A blocked touch still fails those real journey assertions.

Actual native evidence lives under `/Users/emirerben/Documents/Codex/2026-10-29/task-8/evidence/`:

- `gameday-menu-positive.xcresult` and `.log`: three full journey successes, 61.059/54.346/54.562 seconds, verified from ActionTestMetadata.
- `gameday-menu-disabled.xcresult` and `.log`: temporarily disabling only Video fails the guard with enabled=false, hittable=true and finite frame112,569,168,44. The test exits65 before its coordinate tap. This confirms enabled state is required independently of AX hittability.
- `gameday-menu-restored.xcresult` and `.log`: full journey Success in68.515 seconds after product restoration, xcodebuild exit0.
- Original CI artifact: `1931-native-creation-failure.zip`, exported screenshot `1931-gameday-all-attachments/EB941372-8570-4167-9E8B-4A40BDE5A480.png` and matching AX dump `9C4925ED-D805-4527-A159-C6F6F531FD5E.txt`.

Test source SHA256: `ccb2d667a64701885c54a0f2cd2c88e8b1b6b0b481a64a7785acda75517844da`.
Built UI test executable SHA256 (positive run): `647547089f5202b73eb720efb758e32bb29021d514547017c8c4ff0a9d48c5e2`.
Product NativeEditorMediaViews.swift restored SHA256: `f3bdb2f23064dfe93e4f94bea4eea148ff8bbef1fbd0b5ba9c8c52e8b7d0ae79`.

Runs use the task-owned iOS26.5 simulator F0614893-2CB6-447A-A402-322C2F8CB49F and separate DerivedData `/tmp/unattended-gameday-build`. No other simulator/device is touched. Command shape: `xcodebuild -project src/apps/ios/Kria.xcodeproj -scheme Kria -configuration Debug -destination 'platform=iOS Simulator,id=F0614893-2CB6-447A-A402-322C2F8CB49F' -derivedDataPath /tmp/unattended-gameday-build -skipPackagePluginValidation -parallel-testing-enabled NO -only-testing:KriaUITests/GamedayAddClipUITests -resultBundlePath <prefix>.xcresult CODE_SIGNING_ALLOWED=NO test`. Positive run adds `-test-iterations 3`.

These are local simulator results; fresh exact-head hosted CI remains required and physical original-request acceptance is not claimed.
