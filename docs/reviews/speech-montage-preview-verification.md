# Spoken-excerpt montage preview verification

PR #2337 opens the published device recipe for a clean spoken-excerpt montage.
Its video cuts and speech track must match that recipe. Account branding follows
KRI-1533. An exemption removes the watermark and preserves the outro. An account
change or exemption revocation during preparation must discard the pending preview.

## Local results, 19 November 2026

The branch incorporates `main` at `3c07baf74f5de17f3015680b6fe0d8ad28c9649c`.
Checks used synthetic local media, Xcode 27.2 build 27B52, and a dedicated iPhone
17 Pro simulator running iOS 27.2. Provider transport was a fixture. The real
device asset resolver, preview composer, image generator, and editor session ran.

Local compilation required the one-line `Task<Void, Never>` correction from
PR #2399, tracked by KRI-1542. That temporary correction was restored after the
checks. It is absent from this PR's diff. Hosted checks must verify this branch
with the hosted toolchain.

Before the branding correction, all three new regression tests failed. The
exempt frame still contained a watermark. A recipe response arriving after an
account switch or exemption revocation still became the displayed preview.

After the correction, the result bundles report six focused passes and three UI
passes, with zero failures and zero skips in each run.

| Check | Observed result |
| --- | --- |
| `testSpeechMontagePreviewHonorsAccountBrandingAndKeepsOutro` | The exempt frame equals an unbranded reference frame at 0.5 seconds. The ordinary account's frame differs. Both retain the outro duration and exact picture and speech tracks. |
| `testSpeechMontagePreviewDiscardsARecipeLoadedAcrossAccountChange` | A suspended recipe response cannot install a preview after the account changes. |
| `testSpeechMontagePreviewDiscardsARecipeLoadedAcrossExemptionRevocation` | A suspended recipe response cannot install a preview after the exemption is revoked. |
| `testPublishedSpeechMontagePreviewsItsPinnedPictureAndSpeechTracks` | The published picture and speech tracks are the displayed recipe, with no unsaved change. |
| `testPhoneTalkingEditWithClosedLanesAndACardKeepsTheFinishedMP4` | The existing Talking fallback remains available. |
| `testLivePreviewFailureIsReportedWithoutPrivateDetail` | Preview failure reporting retains its privacy checks. |
| `testTextInspectorKeepsEditingAndStyleControlsReachableAtAccessibilityTextSize` | Passed on updated main. |
| `testEditorIsReadyBeforeTheTap` | Passed with the merged #2358 correction. |
| `testSplitPanelTrimCutDuplicateAndUndoKeepPeersExact` | Passed with the merged #2361 correction. |

## Reproduce

Generate the Xcode project with `bash scripts/ios/generate-project.sh`. Use
`xcodebuild test -project src/apps/ios/Kria.xcodeproj -scheme Kria` with a dedicated
simulator destination, `-skipPackagePluginValidation`, `-parallel-testing-enabled NO`,
and `CODE_SIGNING_ALLOWED=NO`. Select the six named tests from
`KriaTests/NativeEditorSessionTests` using `-only-testing:`. Select the three UI
tests from `KriaUITests/AppleTextAccessibilityUITests`, `KriaUITests/EditorUITests`,
and `KriaUITests/NativeEditorInspectorUITests` respectively.

Local evidence is retained under `/private/tmp/nova-2337-branding/` in
`red-controls.xcresult`, `green.xcresult`, and `ui.xcresult`, alongside logs and
result summaries. The committed tests and this record preserve the reproducible
expectations and outcomes.

Physical iPhone verification and production rollout remain pending. No physical
iPhone, production job, customer media, or paid model was accessed in this pass.
