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
`testImproveAndAIActivityStaySeparateDuringPreviewPreparation`. Improve is
available for ready suggestions; it intentionally disappears during a pending
message. The coexistence check delays actual source-pool transport while the
ready suggestions and expanded preparation control are both visible.

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

`NativeEditorPreviewWorkSessionTests/testPlayingInPlaceTitleUpdateSettlesThroughTheRealPlayerSurfaceAndCompositor`
mounts the production preview in a real test-host window. It checks advancing
playback and revision admission without invoking readiness callbacks manually.
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
cycle. The local angle-only `TimelineView` correction awaits native verification;
do not treat the historical test counts as a passing motion acceptance result.

Run these through the editor CI selection or a separately allocated bounded
native batch after launch/doctor, retaining results and captures. The default
`drive editor` still runs only the caption journey named above. Playing generation
handoff, device interruption and encoded-export fidelity require separate proof.
