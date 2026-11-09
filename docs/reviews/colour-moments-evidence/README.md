# Colour moments evidence (KRI-1350)

The exporter test `ColorMomentsCompositionTests.testExportedFramesShowTheMomentOnItsFramesWithTheSoundOnTheDip`
renders a 4 s, 30 fps recipe through the real `AVFoundationLocalExporter`: a black-and-white window 1.0–2.5 s
entered with a light leak and left with a dip to black, a library-style hit on the `sfx` track at the exit
edge, and a 1 s end fade. With `KRIA_COLOR_MOMENTS_EVIDENCE_DIR` set it keeps the MP4 (not committed) and
`exported-recipe.json`.

`scripts/verify-colour-moments.py` then measures the MP4 independently (ffmpeg signalstats + PCM RMS):

- `expect.json` = `expect-from-recipe exported-recipe.json` — the frames the edge contract predicts.
- `checker-report.json` — PASS: flash peak, dip bottom and sound onset at 0 frames of drift; the
  black-and-white run, the end fade 90–119.
- `expect-wrong-control.json` / `checker-report-wrong-control.json` — the same recipe shifted by 3 frames
  (0.1 s). FAIL on the flash, the dip and the sound: the checker rejects wrong timing.

Reference shape: `tests/fixtures/colour-moments/cigdem-reel-answer-key.json` (Çiğdem's reel, measured).
