# Clip dim: export and editor proof (KRI-1608)

Native output on the iPhone simulator (iPhone 17 Pro, iOS 27.2). No physical iPhone.

## Export (proof b)

`ClipDimRenderTests.testADimmedClipExportsDarkerByItsStrengthUnderEveryTextStickerAndDrawing` compiles one editor
draft through the device compiler, exports it with `KriaMediaEngine`'s production exporter and measures the MP4 at
0.75 s against an undimmed export of the same edit. A mid-grey clip sits under every layer a creator puts over it:
a title, pinned corner text, an informative heading (`text_stack_v1`, heading level 2), a song lyric line
(`source_params.origin = "song"`), a caption cue, a pasted sticker (a media overlay) and a drawn stroke. It compiles
the way a device-rendered edit does (`deviceCaptions: true`), so the sticker is drawn below the text, in the same
pass that dims the clips.

```bash
cd src/apps/ios && TEST_RUNNER_KRIA_CLIP_DIM_RENDER_DIR=/tmp/clip-dim-render xcodebuild -project Kria.xcodeproj \
  -scheme Kria -destination "id=<simulator>" test -only-testing:KriaTests/ClipDimRenderTests
```

Each layer is measured on its opaque interior only: the pixels in its own band of the frame whose undimmed luma is
within 6 of the layer's fill. Anti-aliased edges, caption outlines and shadows blend with the dimmed picture beside
them, so they are left out. The picture is a text-free patch right of the pinned text.

| Layer | Pixels | Luma, no dim | Luma at 50% | Ratio at 50% | Luma at 100% | Ratio at 100% |
|---|---|---|---|---|---|---|
| Picture | 54,621 | 126.86 | 63.86 | 0.503 | 0.00 | 0.000 |
| Title | 38,959 | 254.91 | 254.91 | 1.000 | 254.91 | 1.000 |
| Pinned corner text | 5,413 | 218.64 | 218.61 | 1.000 | 218.62 | 1.000 |
| Informative heading | 7,188 | 230.58 | 230.56 | 1.000 | 230.55 | 1.000 |
| Song lyric line | 5,596 | 195.57 | 195.54 | 1.000 | 195.55 | 1.000 |
| Caption | 4,620 | 254.69 | 254.68 | 1.000 | 254.65 | 1.000 |
| Sticker | 71,824 | 72.55 | 72.55 | 1.000 | 72.55 | 1.000 |
| Drawing | 15,322 | 182.05 | 181.72 | 0.998 | 181.49 | 0.997 |

One run on this layout. An earlier layout, with the sticker drawn above the text, gave the same ratios to three
decimals in two identical runs.

The test requires at least 0.98 for every layer. The drawing's small drop is most likely its edge (inferred, not
isolated): video chroma subsampling carries a little of the darker picture into the stroke pixels next to its edge.

**Negative control.** With a temporary line in `RecipeVideoCompositor.render` that dims the whole final frame (text,
stickers and drawings included) whenever a clip carries a dim, every layer assertion fails: each layer reads 0.50 at
50% and 0.00 at 100%, and the picture reads 0.244. So the test does see a dimmed layer. Restored, it passes. (Run on
the earlier layout; the control dims the finished frame, so the layout does not change what it shows.)

## Text behind a dimmed person

`ClipDimCompositionTests.testABehindSubjectTitleHidesBehindADimmedPersonAsItDoesUndimmed` (engine, `swift test`) puts
a behind-subject title over a clip whose left half is a person. The text matte finds the person on the undimmed
picture, as the speaker matte already did, so the title hides behind the same person at 0%, 50% and 100%. Before the
fix the matte read the dimmed frame and the title's left half (2,795 ink pixels) painted over the person at 50% and
100%.

## Editor (proof a)

`NativeDimUITests.testDimHalfApplyToAllRemoveOneSaveAndReopen` reads the live preview from a screenshot: the mean luma
of a text-free patch (the middle half across, 60% to 85% down) went from 114.63 to 59.15 after dimming clip 1 to 50%,
a ratio of 0.516 (two runs, identical). The test requires 0.45 to 0.58. It then applies 50% to all clips, removes the dim from clip 2, saves,
relaunches and finds clip 1 at 50% and clip 2 at 0%.

**Negative control.** With the two tone-curve conversions removed from `RecipeVideoCompositor.dimmed` (a multiply in
the compositor's linear light), the same patch reads 0.731 and the test fails.

`montage.png` shows the three export frames and the editor before and at 50%. In the editor shots, "Your story starts
here" is an editor text layer and stays white; "Alberobello" is burned into the fixture video (`montage.mp4`), so it is
part of the picture and dims with it.

## Engine negative control (first round)

The dim must multiply ENCODED values. With the two tone-curve conversions removed from
`RecipeVideoCompositor.dimmed`, `swift test --filter ClipDimCompositionTests` fails: at 50% the clip pixel
`[200, 120, 40]` reads `[146, …]` instead of `[100, 60, 20]`, the tilt backdrop and the 25% and 75% cases miss by 26
to 56 levels, and the dimmed Talking speaker reads green 191 instead of 128. Run on macOS (`swift test`), 2026-11-24.
