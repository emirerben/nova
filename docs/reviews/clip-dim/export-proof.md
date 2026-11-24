# Clip dim: export and editor proof (KRI-1608)

Native output on the iPhone simulator (iPhone 17 Pro, iOS 27.2). No physical iPhone.

## Export (proof b)

`ClipDimRenderTests.testADimmedClipExportsDarkerByItsStrengthUnderAFullBrightnessTitle` compiles an editor row with
`dim` through the device compiler, exports it with `KriaMediaEngine`'s production exporter and measures the MP4 at
0.75 s against an undimmed export of the same edit. The clip is mid-grey, with a white title in the upper third.

```bash
cd src/apps/ios && TEST_RUNNER_KRIA_CLIP_DIM_RENDER_DIR=/tmp/clip-dim-render xcodebuild -project Kria.xcodeproj \
  -scheme Kria -destination "id=<simulator>" test -only-testing:KriaTests/ClipDimRenderTests
```

| Dim | Picture luma | Picture ratio | Title luma | Title ratio |
|---|---|---|---|---|
| none | 126.88 | 1 | 254.78 | 1 |
| 50% | 63.86 | 0.503 | 254.78 | 0.99998 |
| 100% | 0.00 | 0.000 | 254.77 | 0.99994 |

The title region is the 39,339 pixels brighter than luma 235 in the undimmed export.

## Editor (proof a)

`NativeDimUITests.testDimHalfApplyToAllRemoveOneSaveAndReopen` reads the live preview from a screenshot: mean luma of
its middle half went from 153.2 to 83.5 (ratio 0.545; the region includes the bright title) after dimming clip 1 to
50%. It then applies 50% to all clips, removes the dim from clip 2, saves, relaunches and finds clip 1 at 50% and
clip 2 at 0%.

`montage.png` shows the three export frames and the editor before and at 50%.

## Negative control

The dim must multiply ENCODED values. With the two tone-curve conversions removed from
`RecipeVideoCompositor.dimmed` (a multiply in the compositor's linear light),
`swift test --filter ClipDimCompositionTests` fails: at 50% the clip pixel `[200, 120, 40]` reads
`[146, …]` instead of `[100, 60, 20]`, the tilt backdrop and the 25% and 75% cases miss by 26 to 56
levels, and the dimmed Talking speaker reads green 191 instead of 128. With the conversions in
place all six tests pass. Run on macOS (`swift test`), 2026-11-24.
