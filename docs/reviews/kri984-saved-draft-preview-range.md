# Saved drafts keep valid source endpoints

Cunda's editable preview failed with `RecipeError.invalidTimeline` in the
installed app. Replaying the same sources from a directly constructed editor
document passed. Replaying through `DraftSnapshot.editorDraft` reproduced the
failure before any compiler change.

## Acceptance and root cause

A saved photo or video window must reopen at its original duration and rate,
then decode the expected source frame. A real source overrun must still fail.
No text, source choice, audio policy, or approved timeline may change.

The saved-draft loader computes durations by subtracting clip endpoints. A
1.2-second window after a five-second clip becomes `1.2000000000000002`.
It also supplies a 1.2-second fallback source duration for the photo. The
compiler's strict endpoint comparison rejected that valid window. The same
problem affects a real video whose available duration is exactly 1.2 seconds.

The compiler now uses the surrounding geometry checks' one-microsecond
tolerance at this source-window boundary. It clamps a re-anchored start to
zero. The source interval, playback rate, and output clock keep their existing
rules. The tolerance does not admit a one-millisecond overrun.

## Regression and native evidence

`SavedDraftPreviewRangeTests` creates its own red photo, blue photo, and real
36-frame H.264 video. It uses the real saved-draft loader, session, compiler,
and preview compositor. The passing cases decode a blue frame from the second
source at 5.6 seconds, while preserving the 6.2-second output clock. The color
check rejects a red or black result.

Before the patch, both valid photo and video cases failed with
`invalidTimeline`. The real one-millisecond overrun control passed by refusing
the invalid window. After the patch, all three pass. These checks are included
in the `editor-durable-save` CI journey.

The focused simulator run also includes the neighboring compiler class and
the private Cunda replay through the saved-draft loader. That replay verifies
all 21 original source fingerprints, retains text and standard branding, and
decodes frames across the complete composition. It passed after the patch.

```bash
make ios-generate
xcodebuild -project src/apps/ios/Kria.xcodeproj -scheme Kria -configuration Debug -destination 'platform=iOS Simulator,id=2281FF6C-EE1C-40DC-B061-061BE6FEFF0A' -derivedDataPath .dev/cunda-preview/ios-build -skipPackagePluginValidation -parallel-testing-enabled NO -only-testing:KriaTests/SavedDraftPreviewRangeTests -only-testing:KriaTests/NativeEditorRenderCompilerTests CODE_SIGNING_ALLOWED=NO test
```

The retained run additionally selected the private source-bound Cunda case:
67 tests passed, zero failures. Result: `.dev/cunda-preview/saved-range-green.xcresult`.
The public cases require no account, private media, network, or paid provider.
Use a locally available simulator identifier when repeating the command.

Earlier harness attempts discovered zero tests before Xcode project generation,
then stopped at a test-only API-name compile error. Neither counts as a
regression result. The actual pre-patch run executed three cases and failed
the two valid ranges. Those logs and result bundles remain retained.

## Remaining delivery and launch gaps

This report proves a local native fix. It does not prove that the installed
TestFlight app has recovered. The paired phone's Wi-Fi tunnel was unavailable
when physical verification was prepared. A new human/device connection is
pending. The separate QA app preserves the installed Kria app.

The existing Cunda MP4 and its Photos copy already play, but their intro text
overlaps. PR #1889 separately fixes new contract-text placement and initial
editor projection. This compiler change does not rewrite that published MP4.
The original 28.804-second timing request and the remaining original-media
incident replays stay open. No new planning call, creative approval, production
mutation, manual deployment, archive, or TestFlight upload was performed.

KRI-984 remains open. Launch is not cleared by this report.


## 29 October current-main integration

The original proof file above is historical. Four source hashes changed as main advanced, so it does not bind the current integrated code. The separate integration-proof JSON records refreshed hashes at the merged source revision.

The fresh native simulator replay passed 67 tests, zero failures and skips. It includes valid photo and H264 saved windows, genuine 1 ms overrun rejection and neighboring compiler cases. Result bundle is `/tmp/unattended1890-tests.xcresult`. No new physical-device, private Cunda or installed TestFlight proof is claimed. Independent read-only review found no compiler correctness issue and confirmed durable-save journey registration. Fresh exact-head CI and authorization for the automatic production deployment remain required.
