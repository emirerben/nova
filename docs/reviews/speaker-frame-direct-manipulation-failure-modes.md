# Move and resize the corner speaker by hand — failure modes (written before the code)

Ask (Yasin, 2026-11-05): on a Talk-to-camera green-screen edit (one background or several) where the
speaker is drawn small in a corner, tapping the speaker in the iPhone editor preview selects them, a drag
moves them and a pinch (or a corner handle) resizes them. Mock-up approved in chat before the build.

Builds on KRI-1162 (`docs/reviews/speaker-background-windows-failure-modes.md`, rows S27–S45). Nothing
on the wire changes: `speaker_frame` already is `{width_fraction ∈ [0.2, 1], x_fraction ∈ [0, 1],
y_fraction ∈ [0, 1]}` on the recipe, the editor document and the editor Save, and both decoders check
only that range (a non-preset frame already decodes; the Background tool lights no preset for it). So
there is no protocol bump and no new capability token: `speakerBackgroundWindows` and the editor
capability `speaker_background_windows` already gate who may write a frame. The chat keeps writing
named corners only (S37); free geometry comes only from a finger.

## Decisions that remove whole classes of bugs

- **One frame for the whole edit.** The wire has one `speaker_frame`; dragging while a later background
  window shows moves the speaker everywhere. The inspector says so ("same spot for the whole video").
- **Same pixels while the finger is down.** The live drag shows three stills rendered by the SAME
  compositor from the SAME instruction (everything under the speaker / the framed cut-out alone /
  everything above it) and moves only the middle one with the same affine the compositor uses (uniform
  scale about the frame centre, then translate). No new drawing code to drift from the render.
- **The stored value drives the live picture.** The session clamps every gesture sample to the wire's
  range and the live transform is read back from the stored frame, never from the raw finger, so what is
  shown at release is what Save sends and what the phone exports.
- **Frame-only changes stay in place.** Moving or resizing an existing frame swaps the compositor
  instructions without rebuilding the player (no re-matte, no flash). Adding or removing the frame
  (Full ↔ corner) still takes the full rebuild: the matte reports differently with and without a frame.
- **A full-frame speaker is not selectable.** It would cover the whole canvas and swallow tap-to-expand.
  The corner buttons in the Background tool (and the speaker inspector) put a frame back.

| # | Failure mode | Treatment | Pinned by |
|---|---|---|---|
| SF1 | A tap meant for a caption, title or card over the speaker selects the speaker (or the reverse) | The speaker is the LOWEST preview object (z −1000, below every lane object); taps cycle topmost-first as for every other object. | Tap selection: `SpeakerFrameDirectManipulationUITests.testTapDragAndPinchMoveAndResizeTheSpeaker`. Caption-over-speaker order is by construction (no fixture caption overlaps the corner); device check. |
| SF2 | The speaker is selectable where it cannot be edited (gate closed, cloud variant, read-only draft, no frame, the ending clip where no speaker is drawn) | Object exists only while `canEditSpeakerBackgroundWindows` and a frame is set; its bounds come from the composition, which answers nil where no speaker layer is active at that time. | `SpeakerFrameDirectManipulationUITests.testFullFrameSpeakerIsNotSelectable`, `SpeakerFrameManipulationTests.testSpeakerSelectionBoundsFollowTheFrameAndTheSpeakerClip` |
| SF3 | A gesture writes a frame the server or the engine refuses (width < 0.2 or > 1, centre outside 0..1, NaN) and Save fails | Session setters clamp width to [0.2, 1] and the centre to [0, 1] (rounded to 1/10000) and ignore non-finite input. | `SpeakerFrameDirectManipulationUITests.testDragAndPinchPastTheLimitsSaveAFrameInRange` |
| SF4 | What the finger shows differs from the render after release (preview ≠ export) | Live stills are slices of the real instruction; the slices composited back equal the whole render; the live transform is the stored (clamped) frame; after release the moved still stays up until a still prepared at the new frame arrives. | `SpeakerFrameManipulationTests.testSpeakerSlicesRecomposeTheWholeFrame`, `SpeakerFrameDirectManipulationUITests.testTapDragAndPinchMoveAndResizeTheSpeaker` (saved = reopened) |
| SF5 | The preview rebuilds (flash, re-matte, lost playhead) after every release | `LivePreviewComposition.updateText` accepts a background that differs only in a non-nil → non-nil `speakerFrame` and swaps the instructions; nil ↔ frame still throws `LivePreviewComposition-speaker-background`. | `SpeakerFrameManipulationTests.testMovingTheFrameUpdatesInPlace`, `::testAddingOrRemovingTheFrameTakesTheFullRebuild` |
| SF6 | One drag becomes dozens of undo steps, or none | The preview gesture runs inside `beginDirectManipulation` / `endDirectManipulation` (one transaction); the inspector slider inside `beginEditTransaction` / `endEditTransaction`. | `SpeakerFrameDirectManipulationUITests.testOneDragIsOneUndoStep` |
| SF7 | The cut-out slice is matted after the shrink (haloed, mis-sized mask) | The speaker-only slice goes through the same branch as the whole render: mask on the full-size layer, then `framed` (S27 unchanged). | `SpeakerFrameManipulationTests.testSpeakerSlicesRecomposeTheWholeFrame` |
| SF8 | The cut-out slice draws the room when no mask exists | `.original` with a frame draws the plain speaker small (S27b); `.backgroundOnly` draws nothing in the speaker slice. Same branches as the whole render. | `SpeakerFrameManipulationTests.testSpeakerSlicesRecomposeTheWholeFrame` (scripted no-mask case) |
| SF9 | The safe-zone guide flags every bottom-corner speaker because the body touches the bottom edge | The guide tests the FACE: Vision face rectangle on the speaker-only still at gesture start, moved with the live transform. No face found: the whole speaker box, as for any other object. | The guide showing during a drag: `SpeakerFrameDirectManipulationUITests.testTapDragAndPinchMoveAndResizeTheSpeaker` (`dangerGuideSamples`). The face path needs footage with a face: device check (the simulator fixture clip has none, so it exercises the box fallback). |
| SF10 | Part of the speaker is off-canvas and its handles are unreachable | The selection box is the frame intersected with the canvas (as media cards), so handles stay on screen; the centre may still reach the canvas edge (wire range). | `SpeakerFrameManipulationTests.testSpeakerSelectionBoundsFollowTheFrameAndTheSpeakerClip` |
| SF11 | Playback keeps running under the gesture and the frozen stills go stale | `beginDirectManipulation` pauses and re-prepares the stills (existing behaviour for every object). | existing `NativeEditorSession` contract |
| SF12 | The chat no longer understands a hand-placed frame | The copilot snapshot already carries the fractions; `brief_checks` names a non-preset frame "custom"; `set_speaker_frame` presets and `clear_speaker_frame` overwrite it. Parity row: the free placement is `agent_unsupported` with its reason (S37: presets only from the model). | `tests/kria/test_editor_capability_parity.py` |
| SF13 | An old app build opens a variant with a hand-placed frame | Already decodes (range check only) and draws it; its Background tool lights no preset. No 426 needed. | existing `EditorSpeakerFrame(json:)` / `SpeakerFrame.validate()` |
