# PR 1930 text-entry synchronization

The existing creation test typed before explicitly waiting for the keyboard.
Hosted run 40983249162, job 123832414847, failed its exact `Live text` assertion
at 01:51:34 UTC on October 30 after the five-second deadline expired.
The retained failure excerpt shows typing at 01:51:27 and value polling at
01:51:29. The recovery owner reports the field and preview eventually displayed
the expected text. This evidence supports an asynchronous input synchronization
failure. It does not establish a product text-loss defect.

This change reuses only the inspector-test hunk from recovery commit
`2d9f48f153fa2efcc8de9be0150febe999e229b4`. It waits up to five seconds for the
keyboard before typing and gives the exact-value assertion 15 seconds.
The expected text, preview geometry, animation, gesture, and Undo assertions
are unchanged. A wrong value still fails the exact predicate. No additional
retry or product change is included.

The isolated verification base is merge commit
`3012652298dfef3380180c832e510c888bb3620f`, combining PR 1930 head
`807eed4e0eebf99ee7e568c16259308b9d553450` and main
`e8778777e5485e74c47e1c5e63170063270a1154`.
The edited `NativeEditorInspectorUITests.swift` SHA256 is
`ddd65c41214c7ad1329402537f527fad4d3867de9020ce317b5725df4673eb2c`.
The full file equals the recovery owner's committed inspector source.
The untouched `NativeSpeedUITests.swift` SHA256 remains
`029faa7a2a3a72fed0916587ffc618e52e78a8192be6b7f2cba8c98948c9156d`.

A fresh Debug build uses `/tmp/unattended-keyboard1930-build` and the assigned
iOS 26.5 simulator `F0614893-2CB6-447A-A402-322C2F8CB49F`.
The built and installed XCTest executable both have SHA256
`4788b9b66ec748c133e8723ba765c40b26bf305bdb78e91c90d0dcecb6aacfc0`.
The execution log records the new keyboard wait before `Live text` typing.

Verification command after `xcodegen generate` and `build-for-testing`:

```sh
xcodebuild test-without-building \
  -project src/apps/ios/Kria.xcodeproj -scheme Kria -configuration Debug \
  -destination 'platform=iOS Simulator,id=F0614893-2CB6-447A-A402-322C2F8CB49F' \
  -derivedDataPath /tmp/unattended-keyboard1930-build \
  -parallel-testing-enabled NO -skipPackagePluginValidation \
  -only-testing:KriaUITests/NativeEditorInspectorUITests/testSourcePreviewTextCreationAndAnimations \
  -test-iterations 3 -resultBundlePath /path/to/creation-three.xcresult \
  CODE_SIGNING_ALLOWED=NO
```

All three complete executions passed with zero failures or skips. The actual
xcresult metadata reports `Passed` for repetitions 1, 2, and 3, with durations
57.714, 56.198, and 56.149 seconds. Xcodebuild exited 0. The result metadata
contains a runtime warning about modifying state during view update in each
repetition. That warning did not fail any assertion and is outside this test-driver
change. Private artifacts are retained under
`/Users/emirerben/Documents/Codex/2026-10-29/task-8/evidence/pr1930-keyboard/`.
They include `creation-three.xcresult`, `creation-three.log`, `identity.json`,
`summary.json`, `tests.json`, `exact-test.patch`, build logs, and exact commands. The hosted failure excerpt
is the sibling `pr1930-creation-text-failure-excerpt.log`.

The acceptance claim is limited to this complete native XCTest on the named
simulator and source. No local unpatched baseline was rerun. The existing hosted
failure supplies the incident reproduction. No new wrong-content control,
physical-phone run, export, or production rollout is claimed. Parent review and
hosted CI remain separate publication gates.
