# Native video editing, Save and export

Creators play their cut, change timeline/text/drawing content, save edits, reopen
them and export. The delivered video must agree with the saved state.

## Sub-features

- `editor-playback`: preview clock advances through the real media duration.
- `editor-trim`: trim handles change one clip; Undo restores it in one action.
- `editor-text`: longer text does not change unrelated duration/clip geometry.
- `editor-save`: drawn content survives Save and a new app launch.
- `editor-export`: captured word bars reopen and export to simulator Photos.

## How to get to it (user POV)

- Open a ready video's Open editor action or switch from its chat to Editor.
- Use Play, clip timeline handles, Text/Draw tools, Save and Export.
- From Export choose Save to Photos. Reopen through the project/chat after saving.

## Driving it with XCUITest

Preconditions: Launch/Doctor. These existing tests seed an editor baseline with
`-ui-testing-editor`; this is a partial editor proof, not proof of entering from
every ready-video/chat/gallery route. The creation recipe checks its entry CTA.

```sh
"$KRIA_VERIFY_CONTROL" drive --run "$KRIA_VERIFY_RUN" --feature editor \
  EditorUITests/testNativeEditorTrimHandleShortensExtendsAndUndoesAsOneGesture \
  EditorUITests/testNativeEditorLongTextEditPreservesDurationAndClipGeometry \
  DrawingJourneyUITests/testDrawDoneTrimSaveReopenPersists
```

- **Trim and Undo.** Select `native-editor-clip-1`, drag
  `native-editor-trim-trailing`, then tap `native-editor-undo`. Clip duration
  changes and returns; dragging the leading handle extends the clip.
- **Text.** Edit through `native-editor-tool-text`; the longer title appears,
  while original clip values and `native-editor-duration` stay unchanged.
- **Save/reopen drawing.** Select `native-editor-tool-draw`, draw two strokes on
  `native-editor-draw-canvas`, tap `native-editor-draw-done`, trim the drawing,
  then tap `native-editor-save`. A fresh app launch retains its
  `native-editor-timeline-drawing-…` block and Save is disabled. Inspect the
  action/reopened-state screenshots; persistence is provided by a stub transport.
- **Export.** In a separate new run use the same `--feature editor` with
  `EditorUITests/testCapturedCreationWordsPlayReopenAndExport`. The existing
  captured draft supplies 12 word bars. It checks `native-editor-play-pause`,
  `native-editor-current-time`, app reopen, `native-editor-export` → Save to
  Photos, and `native-editor-export-state`. Inspect the result attachments and
  the created video asset/file before making an output claim.

## Gotchas

- Save/reopen drawing uses the real commit request with a stub persistence
  transport; it does not prove authenticated server storage.
- Direct editor fixture launch does not cover creation dispatch, chat/gallery
  entry or production gates. Keep those entry points unverified until driven.
- Export's captured draft is a checked-in baseline, not fresh model output. For
  compiler-to-native output, additionally use the KRI-524 generator/native
  render recipes in the iOS runbook; their setup requires the API test Python.
- The helper exports XCTest attachments, not a Photos-library video extraction.
  A Saved to Photos screenshot alone leaves media content/order/audio unverified.
