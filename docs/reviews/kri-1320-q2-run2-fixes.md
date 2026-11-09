# KRI-1320: Qendresa Q2 run 2 fixes

Parent incident: KRI-1320 (Qendresa Q2 run 2, prod thread `9db44dc2`, chat `qe2`, 2026-11-08).
Draft job `4620fd7c`, render job `1d512b9c`, TestFlight 1926 (protocol 45). Kit scorer:
`kria-qendresa-test/tools/check_result.sh <mp4> Q2 --ignore-tail-s 1.6 --labels
ALL_IN_ONE/truth/labels_expected.json`.

The Approve 409 (KRI-1321) was fixed separately in #2226 and is not part of this change.

## Acceptance

| Issue | What she saw | Expected after this change |
|---|---|---|
| KRI-1322 | "That exact text isn't in this draft" for "the GitHub logo above it"; the first draft was held | A logo named next to the title is the uploaded picture: a render promise, never missing text; the planner adds the title-anchored logo beat |
| KRI-1323 | "Make it without these" came back as the same hold card | The tap survives a wording question asked before the draft |
| KRI-1286, KRI-1324 | Kria asked her to approve her own quoted ending (`comment REPO ➔`), and ➔ cannot be drawn | Her quoted ending is hers without a question; Kria never offers an undrawable glyph |
| KRI-1239 | The iPhone app froze and was killed (0x8BADF00D) after "Make it without these" | No main-thread block of 500 ms or more, and the scroll loop settles |
| KRI-1305 | The 3-line title rendered on top of her captions (`text_overlap: true`) | The title squeezes between her face and captions; only if that cannot fit do captions wait under it |
| KRI-1284 | Numbered labels were faint grey, moved per row, and label 5 went under her face | One cream, outlined look and one shared spot clear of the face and captions |
| KRI-1307 | Black bars above and below her README recordings | Recordings fill the frame (zoom floored at the cover fit), aimed at the text column, and read as zoomed |
| KRI-1288 | 3 pauses over 0.35 s after "Cut all my pauses" (0.43, 0.37, 0.55 s) | No pause over 0.35 s; every kept word still heard |
| KRI-1304 | A draft green-screen row said Done while the lead said a recording was left out | A row is never Done when a recording is left out; a pending zoom is "Will do at render" |

## Evidence (offline replay of her real run data; no paid model calls)

Each fix has a test written first that fails on `origin/main` (0e4ff67a3) and passes on this branch.

- Server tests (`cd src/apps/api && .venv/bin/python -m pytest <file>`):
  - `tests/kria/test_kri1322_title_logo_picture.py`
  - `tests/kria/test_kri1323_simpler_survives_wording_question_postgres.py` (real Postgres)
  - `tests/kria/test_kri1324_wording_glyphs.py`
  - `tests/pipeline/test_tight_pause_cut_gated_take.py` (her uploaded AAC stream, bit-exact)
  - `tests/pipeline/test_spoken_list_labels_one_look.py` (her run's caption cues)
  - `tests/tasks/test_title_clear_of_captions_q2_run2.py` (her run's face and caption geometry)
  - `tests/services/test_background_focus_fills_frame.py`, `tests/kria/test_background_zoom_cover_aim.py`
  - `tests/kria/test_kri1304_draft_row_matches_lead.py`
- Red on `origin/main`: 28 failed, 17 passed across the first set of new files; the later
  review follow-ups (squeeze, cover aim, zoom promise) each failed on main before their fix.
- Full API suite on the branch: 0 new failures. The 35 failures and 83 errors are identical on
  `origin/main` (render-review, slide-post Postgres, face sampler, tesseract export,
  Apple revocation: environment).
- Reliability gates mapped to the changed paths: 1585 passed, 0 environment skips.
- KRI-1288 replay: pauses over the limit 3 to 0, longest 0.55 to 0.30 s; whisper.cpp heard all
  110 kept words.
- KRI-1305 geometry: title squeezed to 0.72 between her chin (0.6175) and captions (0.7684),
  smallest cap 54 px, every caption kept (predicted captions_on about 0.81 without a logo).
- KRI-1239 (iOS): `KriaPerfUITests/HeldDraftStallUITests` replays her thread offline
  (`KRIA_CHAT_REPLAY=held-draft`) on an optimized build, iPhone 16 Pro simulator, iOS 26.5.
  Original code: 1098 ms block and 120 scrolls, and in another run an endless layout loop.
  Fixed: 171-202 ms worst block, 46-68 scrolls, 6 runs without a loop. Needs a quiet Mac; it is
  not part of `bench.sh` or CI.

## Product trade-off

On a close selfie, a logo above the title, the title and her captions cannot all fit. As built,
the logo wins and captions wait while the title shows (predicted captions_on about 0.72, below the
kit's 0.80 rule). Without a logo, the squeezed title keeps every caption.

## Gaps

- No fresh phone run yet. The kit's done criteria (no hold, one Approve, video in under 5 min,
  scorer at least 13/15, labels 5/5, text_over_face at most 0.02, no black band) need a re-run
  of the Q2 prompt on a build with this change, after deploy.
- The iOS freeze is proven on the simulator only; the iPhone 15 with production thought sizes
  is not checked.
- While Kria thinks, each 1 s poll still redraws about 20 rows (100-200 ms on the simulator).
