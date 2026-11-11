# Native editor

## Sub-features

Caption editing, document dirty state and Save availability. Source is
`Kria/Features/NativeEditorSession.swift` and
`Tests/KriaUITests/NativeEditorInspectorUITests.swift` under `src/apps/ios/`.

## How to get to it (user POV)

Open a ready edit, select a caption, change the text and approve the line.

## Driving it with XCUITest

Run `drive editor`. The helper selects
`NativeEditorInspectorUITests/testApprovingACaptionLineCommitsItAndEnablesSaveOnceThePanelCloses`.
The new line must commit and Save must become available after the panel closes.
Also run `NativeEditorSessionTests/testReadyIndexAbsentFromFreshPoolPreservesDocumentAndPlacement`
when investigating imported clips or source-pool mismatches.

## Gotchas

The selected UI check stops at Save availability. It does not prove Save,
reopen or final export. Those require their original recipe and actual media.

## Editor activity and preview handoff

The editor UI catalog also selects the `CreationUITests` completing-activity
captures, rejection recovery, repeated requests/background return, and
`testTheEditorHasNoImproveButtonAndItsChatSheetOffersTheSuggestionChips`. The editor has
no Improve button; ready suggestions are AI chips in the chat and in the editor's chat
sheet, and a tap adds the chip's prompt to the composer. The check delays actual
source-pool transport while the ready suggestions and expanded preparation control are
both visible.

`testChangedEditorPreviewRetainsOldPictureAndBlocksExportThenSupportsManualUndo`
uses an opt-in same-target generation change. The next generation is still
rendering; a current native source preview does not claim encoded-render
completion. Inspect the captured old, retained, current and manual/Undo pictures.
Accessible text and a ready layer alone do not prove their displayed pixels.
This case passed at `017f2a5e`, but its actual pictures exposed a retained-picture
layout defect: the notice shrank the picture and sat behind the activity control.
At `9a2b7203`, the corrected full-picture/top-notice overlay and overlap checks
passed. Actual OLD, retained, NEW, deselected manual-note and Undo screenshots were
inspected: the picture keeps its scale and the notice remains readable.

`NativeEditorPlaybackAdmissionUITests/testPlayingTitleUpdateSettlesThroughTheRealSurfaceAndCompositor`
launches and activates the DEBUG playback fixture before mounting the production
preview. The in-app sequence checks advancing playback and revision admission
without invoking readiness callbacks manually. It is mapped to both editor and
smoke CI groups. This replaces the unit-host version, which passed locally but
failed in hosted CI before playback because no foreground `UIWindowScene` existed.
The replacement passed its allocated cold-app UI run at `8cc5851d2` on Xcode 26.6 /
iOS Simulator 26.5, with all foreground, frame-admission, identity and playback
assertions. Its actual screenshot visibly contains the updated title. Hosted
Xcode 26.3 and integration onto the approved baseline repair remain pending.
`NativeEditorSessionTests/testRetainedPreviewStopsOnSourceFailureAndRetriesWithoutLosingTheUnsavedTrim`
checks failure/retry status and authored-state preservation; the deferred-conflict
case checks export refusal after the candidate composition is ready but its
previous picture is still presented. Playing admission and the deferred-conflict
export control passed at `017f2a5e`. The failure/retry fixture timed out; stable
source identities and request/adoption prerequisites passed at `9a2b7203`, along
with the real failure/retry path. All 36 selected state/session and nine UI cases
passed with zero skips at that revision. Improve's
Done action must be selected by its actual visible label because the sheet's
container identifier masks the child identifier in the exported hierarchy.

Continuous motion must also be inspected through keyboard dismissal, sheet return
and status changes. The `9a2b7203` normal recording exposed an outline that drifted
away from its control for several seconds despite aligned stills and passing UI
checks. Its `PhaseAnimator` interpolated layout over the decorative eight-second
cycle. At `e11db5a7`, the qualified angle-only `SwiftUI.TimelineView` correction
passed build/doctor, all 36 state/session and nine UI cases with zero skips.
Temporal samples from the actual recording through keyboard/sheet dismissal,
Sending-to-Working and settlement showed the outline attached; the old recording
remains a negative visual control. This is sampled recorded-motion inspection,
not frame-by-frame playback. The actual excerpt is Library
`libfile_cbed66fd87688191a6b11e1b0b23e65e`; it proves activity feedback and
settlement, while the separate changed-title case proves preview replacement.

Run these through the editor CI selection or a separately allocated bounded
native batch after launch/doctor, retaining results and captures. The default
`drive editor` still runs only the caption journey named above. Playing generation
handoff, device interruption and encoded-export fidelity require separate proof.
