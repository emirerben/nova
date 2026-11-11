# KRI-1342 item 7 + KRI-1346: corner label and closing card on phone Talking edits

Parent incident: KRI-1342 (Kria test "yorum3", 2026-11-09, thread d9d7ad3d, job 7eba9903,
Fly 4f6ed3014, client protocol 46). Request (creator's words):

> A small "Yorumla" label in the top left for the whole video. End on a red screen with yellow
> text "follow for more brand and marketing content".

## What went wrong (raw evidence, prod admin debug, read-only)

| Ask | Brief (typed facts) | What happened | Where it was lost |
|---|---|---|---|
| Label | r4 `text/global`, literal "Yorumla", `position: top_left` | Became `opening_title` for 0-1.9 s at left-middle (x 0.06, y 0.675); review said "1 of 1 don't have the requested position" | `strategy_policy._talking_top_text_as_title` (KRI-1009): Talking had no corner-text lane |
| End card | r5 `text/global`, `closing`, `text_color #FFFF00`, `background_color #FF0000` | Draft held: "Putting a plain colour behind you isn't available on your iPhone yet"; creator took the simpler version without it | `brief_checks.asks_speaker_background` read any `background_color` as a speaker background |

## Failure modes written before code

See the list in the PR body (L1-L15, C1-C14). The guard tests cite them.

## Design

- **Label:** the planner already records corner text as `pinned_texts`. Behind
  `PHONE_TALKING_CORNER_TEXT_ENABLED` the Talking edit keeps the pins and the worker draws them
  (`app/pipeline/phone_talking_corner_text.py`): same row builder as every other writer, placed
  off every face sampled across the row's whole window, inward then smaller, dropped when no spot
  clears the face. Whole-video rows run to the real end. No new recipe field.
- **End card:** behind `PHONE_TALKING_CLOSING_CARD_ENABLED` + verified `freezeFrame`,
  `visualBlocks`, `closingCard` (server-only qualification token): the last main clip holds its
  last frame (silent on the device) under a solid `closing-card` visual fill, with the words as one
  text row timed to the card (`app/pipeline/phone_talking_closing_card.py`,
  `compile_phone_subtitled_plan(closing_card=)`). No new recipe field, so no protocol bump.
- **Routing fix (always on):** a text ask with `closing` + `background_color` is a closing card,
  never "a plain colour behind you".
- **Capability for honest replies:** `phone_rollout.phone_talking_corner_text_supported()`,
  `phone_talking_closing_card_available()` / `_planned_for(protocol)`.

## Native evidence (iPhone simulator, real engine)

Filled in below from `scripts/ios/talking-label-endcard-render-e2e.py` + an independent frame
check, `scripts/ios/talking-label-endcard-check.py` (ffmpeg 5 fps + Apple Vision OCR/face
boxes; audio envelope vs the take).

Take: the Yorum3 take itself (IMG_7212 from the Çiğdem kit, 1080x1920, 40.12 s, a stand-in take),
whisper.cpp word times from the kit; same request words. Simulator: iPhone 17 Pro Max, iOS 26.5,
`DeviceMontageRenderE2ETests/testTalkingHeadCutawayRendersOnTheIPhone` — both modes
`** TEST EXECUTE SUCCEEDED **` (the after mode also asserts the card's red at two pixels).
Frame check: 5 fps, Apple Vision OCR + face boxes on every frame (faces were found on every
frame, x 0.32-0.69, y 0.30-0.50); audio: 100 ms envelope of the export vs the take.

| Check | Before (today, flags off) | After (flags on) |
|---|---|---|
| "Yorumla" read at the top-left corner | 0 / 201 frames (big title at y 0.57 for 0-1.9 s) | 201 / 201 frames |
| Label box over a detected face | — | 0 / 201 frames |
| Video length | 40.12 s | 43.12 s (talk 40.12 s + 3 s card) |
| Frames after the last word that are red edge to edge | — (no card) | 13 / 13 |
| Card words read on those frames | — | 13 / 13 |
| Speech up to the end of the take (envelope correlation with the take) | 1.000 | 1.000 |
| Last word's energy kept (export / take) | 0.994 | 0.994 |
| Sound during the card (RMS) | — | 0.0 (silent) |

Wrong-content control: the "before" export (title instead of a label, no card) fails the label
check (0 %) and has no card frames, with the same take, words and checker. Media stays out of git
(local scratch: `e2e-before/`, `e2e-after/`, `before_after.jpg`).

## Remaining gaps

- Physical-iPhone preview + export check before `closingCard` joins the verified list (owner:
  KRI-1346).
- Corner text face sampling covers the first speaker's take only (multi-clip Talking, inserted
  clips and faces inside full-screen photos are not sampled); speaker-background frames are not
  mapped; marked objects (KRI-827) are not avoided by the label.
- The iOS editor's own preview compiler is not taught the card; the server Save keeps it.
- The planner may still also write a speaker background for the colour (not verified: no paid
  model run). The brief check no longer refuses on that account.
