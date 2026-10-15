# KRI-670: Reels and TikTok preview guide

The iPhone video preview shows a combined approximate guide during direct text
and media manipulation. Margins are product defaults: top 10%, bottom 25%, left
5%, right 15%. They are advisory, not platform guarantees. TikTok zoom research
is deferred by the creator's scope decision.

The guide uses live text geometry and a pinned media footprint, including scale
and rotation. Visual media blocks now resolve their rendered media bounds. Small
media centers use the same center-versus-corner discrimination as text, avoiding
accidental rotation when the entire overlay is within the corner grab radius.

The guide is view-local, ignores hit testing, and disappears on gesture end,
cancellation, backgrounding, and navigation. It never enters saved drafts or
render recipes. Fullscreen footage, playback-only views, slide posts, and web
editing are outside this change.

## Acceptance and evidence

Failure scenarios were recorded before implementation: center-only detection,
rotated edges, asynchronous media rebuilds, canvas-size changes, interruption,
and interference with dragging/undo/export.

- `NativePreviewDangerZoneTests`: each edge, exact boundary contact, rotation,
  enlargement, live media translation, and canvas-size invariance.
- `NativeTextTransformLayerTests`: existing shared transform regression coverage.
- `testPreviewDragIsOneUndoableTextEdit`: real text drag into each of the four bands,
  release cleanup, and undo; screenshots attached to the result bundle.
- `testSourcePreviewTextCreationAndAnimations`: live guide updates during corner
  scale/rotation, oversized pinch warnings, and release cleanup.
- `testVisualControlsEditAndPlaybackRemainAvailable`: real media drag into the
  four bands and back inside, release cleanup, and subsequent editing/playback.
- Initial focused run: 18 unit tests and 2 UI tests passed. Active-guide recording
  inspected for warning and safe states; fixed a vertically centered guide and
  clipped label found during the first visual pass.

After integrating `c9e399211865` from main, all 1,758 native tests completed
with 24 environment-dependent skips and zero failures. The final four-test
UI rerun passed text/media boundary gestures, live scale/rotation feedback,
release cleanup, undo, and panel dismissal. The preship gate passed; no incident
reliability gate maps to these paths. The broader UI run remains recorded
separately because its binaries predate this base update.
The guide's lifecycle cleanup is covered by code review; a mid-gesture hardware
background interruption has not been manually exercised.

## Reproduce

Run `make ios-generate`, then `KRIA_SIMULATOR_ID=<iPhone-UDID> make ios-verify`.
The simulator's name must start with `iPhone`, even with an explicit UDID.
Focused XCTest selectors:

- `KriaTests/NativePreviewDangerZoneTests`
- `KriaTests/NativeTextTransformLayerTests`
- `KriaUITests/NativeEditorInspectorUITests/testPreviewDragIsOneUndoableTextEdit`
- `KriaUITests/NativeCaptionVisualUITests/testVisualControlsEditAndPlaybackRemainAvailable`

The UI tests read the existing test-only preview accessibility value to check
samples captured during synchronous XCUITest gestures, then assert the guide is
gone on release. They do not add a production control or persist diagnostics.
