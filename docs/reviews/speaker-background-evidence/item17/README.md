# Qendresa item 17: simulator export (2026-11-05)

The real phone Talking recipe (compiled by `scripts/ios/phone-background-windows-e2e.py` through
`_strategy_background_request` → `_resolve_strategy_background` → `compile_phone_subtitled_plan`)
rendered on an iPhone 17 Pro simulator (iOS 26.5) by
`DeviceMontageRenderE2ETests.testSubtitledBackgroundWindowsRenderOnTheIPhone`, then scored with the
Qendresa kit's `check_result.sh … Q2 --ignore-tail-s 1.6`. Footage is a synthetic stand-in (TTS
walkthrough over a stock talking shot, five drawn "screen recordings"); none of it is in git.
Per-case data: [`simulator-windows-results.json`](simulator-windows-results.json).

What is on screen (OCR of the exported MP4, 2 frames/s), seconds:

| Case | Recording on screen | Matches the recipe |
|---|---|---|
| Flag off (today), names as spoken | 1: 0–12 · 2: 12–17.5 · 3: 17.5–23.5 · 4: 23.5–30.5 · 5: 30.5–34.5 · **1: 34.5–38.5 · 2: 38.5–43** | yes (≤0.5 s) |
| Flag on, names as spoken | 1: 0–12 · 2: 12–17.5 · 3: 17.5–23.5 · 4: 23.5–30.5 · **5: 30.5–43** | yes (≤0.5 s) |
| Flag on, her typed labels | 1: 0–12 · 2: 12–17.5 · 3: 17.5–23.5 · **4: 23.5–43** (5 dropped) | yes (≤0.5 s) |

- Each recording starts on the word that names it; recording 1 also covers the lead-in before its
  name (the existing first-window rule).
- Flag off reproduces the bug (recordings 1 and 2 cycle back after the last one). Flag on: the last
  recording holds to the end. This run predates the merge with #2118 (no photo twice, S57): on
  main the flag-off layout also holds recording 5 to the end, so the "flag off" row above is the
  pre-#2118 baseline, not what main renders now (not re-run on the simulator).
- Typed labels: the stand-in voice's "Claude" is transcribed "Clawed", which no matcher (exact,
  KRI-961 near-hearing, item 17 loose/plural) hears as "Claude code", so recording 5 is dropped as
  `phrase_not_heard`. The planner is taught to copy the transcript's spelling; her real take reads
  "cloud code game studios", which the plural retry does hear (unit tests).
- Kit rules item 17 owns pass in all three: `screen` 1.0 (≥0.8), `corner_speaker` 1.0 (≥0.7).
  Failing rules belong elsewhere or to the stand-in footage: `length` (41.5 s stand-in vs her
  60–135 s), caption words (item 12), ending (item 14), and `rhythm` (her recordings scroll
  constantly, 0.55 s median; the drawn stand-ins scroll slowly; a held last recording lengthens the
  gap by design).
