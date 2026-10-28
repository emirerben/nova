# Native editor gesture verification

This corrects two UI checks found while delivering KRI-984 through PR1892.
It changes test driving and the warning oracle. App, renderer, fixture transport,
capability policy, and accepted speed values are unchanged.

## Pinch warning: check the phase that can be unsafe

CI 40668700336 on `cd0ed23e544ab5ba4693aba4682a44ceb6535501` failed
`testSourcePreviewTextCreationAndAnimations` in three automatic attempts. The
retained screen recording shows the orange warning during enlargement at 55.5s,
then a cleared guide after shrinking at 74s. The test checked only that final
safe sample and therefore rejected working behavior.

The same assertion failed on unchanged main
`53dc75b3b78decc7f292af647c136295bece7c78`. A temporary diagnostic retained
the actual values after each gesture: enlargement reported
`bottom,left,right,top`; shrinking reported no unsafe edges. The corrected
check requires an available, unsafe sample after enlargement and an empty
sample after shrinking. It still checks that the live guide disappears after
the gesture, that text samples were produced, that the preview stays available,
and that the animation controls and cancel flow work.

A separate local run missed the initial Text tap before the new-text input
appeared. The recording preserves that startup state. The test now waits for
the prepared video and an enabled, hittable Text control with finite, stable
bounds, using the existing 12-second readiness budget. It taps once; the
new-text input still has the original 3-second outcome deadline. The exact
cause of that occasional early miss remains unconfirmed.

## Speed: drive the endpoint while keeping exact assertions

The same CI run saw 3.95x instead of 4.00x and 0.40x after undo in an earlier
attempt; that test recovered on retry. Unchanged main passed it locally.
Xcode's public `XCUIAutomation/XCUIElement.h` slider API describes normalized
adjustment as best effort and does not guarantee an exact position.

The test now uses one physical thumb drag to each end, starting between the
inset thumb centres and ending beyond the track. It retains exact 0.25x/4.00x
and 24.0s/1.5s checks for the six-second fixture, then exact reset and undo
checks. It adds no action retry or numerical tolerance. The precise cause of
the recovered CI undo difference is not independently established; this is a
verified test-driving correction, not a claimed production speed fix.

## Evidence and failed iterations

The final two complete UI journeys passed on iPhone 17 / iOS 26.5 with zero failures
and zero skips. This is two unique tests. No unsupported-speed UI case was added.
Original CI used iPhone 17 Pro / iOS 26.2 and remains a mandatory fresh gate.

Retained CI artifact: [12811698140](https://github.com/emirerben/nova/actions/runs/40668700336/artifacts/12811698140).
Its underlying unit suite had 2086 executions, 26 skips and zero failures. The
selected UI suite had 88 unique tests: 87 passed, one failed after retries. The
successful Gameday creation replay is retained separately in artifact 12807399359.

Failed local iterations remain available: the original pinch assertion; the
first endpoint driver missing the minimum; its measured touch geometry; a
later early Text tap; and an introduced XCTest expectation-registration error.
That helper now uses an unregistered `XCTNSPredicateExpectation`, so the later
text-entry wait cannot wait on it twice. None of these failures was counted
as a successful replay or erased by a workflow rerun.

Final result: `gesture-replays-final.xcresult` and its summary/proof in the task
artifacts. Summary SHA-256:
`314b7b727e754982efb5973ee4c79bd30fb297a1f39e25e44d3e3f8aaf584bbd`.
Bundled font-set SHA-256:
`0ca78ba68bffb6ee34feba289241c42eb9eeecad219e6a2617409f2352e1df6d`.

The two exact test-source SHA-256 values are:

- `NativeEditorInspectorUITests.swift`: `9606a9f5d33b736f59626d7f08d1f5c11fe4874f820dc03cbad5f6d61bdabd35`
- `NativeSpeedUITests.swift`: `8f7102726f19ba52417c34d89dfd5f0f9bea33587a1717355283f1b9eba9039b`

```bash
make ios-generate
xcodebuild -project src/apps/ios/Kria.xcodeproj -scheme Kria -configuration Debug -destination 'platform=iOS Simulator,id=0C93EF74-5406-4416-9CD5-E692F0BA7495' -derivedDataPath .dev/native-gesture/ios-build -skipPackagePluginValidation -parallel-testing-enabled NO -only-testing:KriaUITests/NativeEditorInspectorUITests/testSourcePreviewTextCreationAndAnimations -only-testing:KriaUITests/NativeSpeedUITests/testDeviceClipSpeedAdjustsResetsAndUndoesThroughTheInspector CODE_SIGNING_ALLOWED=NO test
```

Use an available local simulator identifier if the retained device is removed.
The scope guard acquired for these UI test files was released with its retained
owner token. No previous boundary or mutation lock was removed.

Fresh CI, current-main integration, and review checks still gate merging. This
report does not clear the Cunda TestFlight candidate, its corrected published
text, original 28.804-second timing request, or remaining original-media replays.
