# KRI-1032 native copy parity

Local fix, 28 October 2026. Parent KRI-1023 remains responsible for the private
Plaza M1 rerun on a physical iPhone and rollout. No production or physical-device
claim is made by this evidence.

Acceptance: the editor's real source preview and exported composition retain the
pinned server fixture's muted slots, landscape bars and text anchors; an unedited
Save/Share selects the finished render file. A trim durably saves both timeline
and rippled text, makes that render stale and keeps export gated while its new
render is pending.

The native journey initially could not compile (await inside XCTest's autoclosure),
then hung in its synthetic AVAssetWriter (inputs finished only after both streams
were exhausted), then lacked the cold preview's no-bed device-status response.
These transport/fixture failures were repaired without replacing the real session,
resolver, compiler, composition, exporter or download routing.

Once running, the journey exposed a product mismatch: unified-cut-3 has a 0.96 s
source and a 0.966667 s slot. The editor excluded every held clip from letterboxing;
phone_guided_plan applies fit before adding the hold. The compiler now receives the
guided-story identity and preserves its bars through the hold. Other formats keep
their existing eligibility rule. The failing native assertions (scale 1 instead of
0.31640625; top-band luma 219.808 instead of black) were retained and now pass.

Run from the integrated verification worktree:

```sh
xcodebuild -project src/apps/ios/Kria.xcodeproj -scheme Kria -configuration Debug \
  -destination 'platform=iOS Simulator,id=C11D5CF8-41A2-4457-8073-0DB612BD949A' \
  -derivedDataPath .dev/ios-build -skipPackagePluginValidation \
  -parallel-testing-enabled NO \
  -only-testing:KriaTests/KRI1032MontageCopyParityE2ETests \
  CODE_SIGNING_ALLOWED=NO test
```

Result: TEST SUCCEEDED, journey 3.703 s. Generated originals have the incident's
shapes/lengths and an audible tone. All three landscape top bands measure luma 0;
portrait edges and all centres exceed 100. Muted windows measure PCM RMS 0;
Recoleta measures 0.366. Text anchors match the pinned fixture. The six-frame
montage was visually inspected: bars, title, labels and watermark are visible.
Generated footage proves composition fidelity, not identification of the private
incident's subjects. The 0.167 s slot is covered by recipe volume checks but omitted
from file-window RMS sampling, which requires a 0.1 s margin on each side.

Retained locally under the task's visualization artifacts:
`yasin-copy-parity-proof/{report.json,montage.png,editor-copy.mp4}`. Test result bundle
and full logs remain in `/private/tmp/yasin-night-review/copy-parity-final.xcresult`
and `copy-native-final.log`. No media files or private footage are committed.
