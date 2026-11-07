# KRI-1264 verification: zoom and animation on a phone Talking card

Acceptance and failure modes: [failure-modes.md](failure-modes.md).

## Root cause

Three places dropped the style, and a fourth would have failed the recipe:

1. The server clamp keeps `visual_editor_style` closed on every device variant, so the editor disabled Zoom and the
   Animation tab on a Talking card (`_clamp_phone_editor_capabilities`).
2. Save refused any card style but the angle (`_card_rotation`), because `SubtitledOverlayCard` had nowhere to keep it.
3. The Talking compile wrote only `transform.rotation_degrees`, and the recipe placement had no style field.
4. The device could not decode a placement style from a recipe at all. `RecipeJSON` decodes with
   `.convertFromSnakeCase`, which turns `rotation_deg` into `rotationDeg` before the lookup, so `VisualEditorStyle`'s
   explicit `rotation_deg` key never matched and the whole recipe threw `keyNotFound`.

## Failing repro, then the fix

Before the engine fix, `swift test --filter PlacedCardStyleTests` (engine package, macOS) failed 6 of 6:

```
PlacedCardStyleTests.swift:47: error: -[KriaMediaEngineTests.PlacedCardStyleTests testZoomScalesTheCardAboutItsCentreAndFourTimesIsCroppedNotRefused] : failed: caught error: "DecodingError.keyNotFound: Key 'rotation_deg' not found in keyed decoding container. Path: visualPlacement.editorStyle. ..."
Executed 6 tests, with 6 failures (6 unexpected)
```

After: `Executed 6 tests, with 0 failures`. The server side failed before its code existed (Save refused zoom with a
422, `test_a_card_style_with_anything_but_an_angle_is_refused_not_dropped`, and the device map pinned
`visual_editor_style` closed).

## Checks run

| Check | Command | Result |
| --- | --- | --- |
| Server card style | `cd src/apps/api && pytest tests/routes/test_overlay_card_style.py` | 36 passed |
| Every test file touching the changed modules (153 files) | `pytest -n 8 <files>` | 5685 passed, 34 failed; the 33 in `test_phone_guided_dispatch.py` and `test_kri1192_caption_render.py` fail the same way on base `a518aed8c` (local ffmpeg without libass, local DB). The 34th was the parity test reading a Swift file mid-edit; it passes alone. |
| Engine pixels | `swift test --filter "PlacedCardStyleTests\|PlacedCardTiltTests\|VisualEditorStyleTests\|NativeCompositionTests"` | 32 passed |
| iOS session (iPhone 17 simulator, iOS 26.5) | `xcodebuild test-without-building -only-testing:KriaTests/NativeOverlayCardStyleTests -only-testing:KriaTests/NativeOverlayCardTiltTests -only-testing:KriaTests/NativeVisualAuthoringTests -only-testing:KriaTests/OverlayGenerationTests ...` | 9 + 8 + 19 passed; OverlayGenerationTests passed except one export proof that stalled on the loaded simulator (it exports a visual block that carries no wire style) |
| Contracts | `python -m app.cli.kria_contracts --check` | passes after `--write` (OpenAPI gains `VisualMediaPlacement.editor_style`) |
| Lint | `ruff check` / `ruff format --check` on the touched files | clean |

## Device chain (simulator)

`src/apps/api/.venv/bin/python scripts/ios/card-style-e2e.py all OUT --simulator UDID` runs the real chain: the
server's editor projection of a plain card, the iPhone editor session (Zoom 2, Pop), the Save payload it sends, the
server's editor Save and Talking compile, the pinned recipe, the device's recipe decoder and production exporter, and
the MP4. The score step reads frames and checks the card against the acceptance above.

Run on an iPhone 17 simulator (iOS 26.5), 2026-11-07. The app's Save sent
`editor_style: {zoom: 2, animation: {entrance: "pop"}, rotation_deg: 0, fit_mode: "cover"}`, and the server pinned it as
the card clip's `visual_placement.editor_style`. Card box measured in the exported 1080x1920 MP4 (width in pixels, centre,
green level at the centre):

| Case | 0.5 s | 1.1 s (0.1 s into the card) | 2.0 s | 3.5 s |
| --- | --- | --- | --- | --- |
| plain | no card | 378 px, (539.5, 767.5), 202 | 378 px, (539.5, 767.5), 202 | no card |
| zoom2_pop | no card | 660 px, (539.5, 767.5), 134 | 756 px, (539.5, 767.5), 202 | no card |

All 10 checks pass: the plain card is 0.35 of the frame wide (378 of 1080) and still; the 2x card is 2.000 times as
wide about the same centre once settled; 0.1 s into the Pop it is 0.873 of its settled width (the 0.4 s Pop eases from
0.7 to 1) and fainter; neither card is drawn outside its 1.0 to 3.0 s window. The plain case is the control: a zoom that
was dropped anywhere in the chain scores a ratio of 1.0 and fails.

The montage (`OUT/montage.png`) stays local: media is not committed.

## Not verified here

- A physical iPhone. The flag stays off until a device check of a 2x Pop card, alone and tilted, in the editor preview
  and in the export.
- `make verify-phone-render`: the TLA+ jar it downloads did not match the pinned v1.8.0 hash on this machine, so the
  model did not run. `_client_update_detail` (one more 426 feature, the same "below the floor" rule the model abstracts)
  is new coverage drift; 36 other covered functions already drift on base.
