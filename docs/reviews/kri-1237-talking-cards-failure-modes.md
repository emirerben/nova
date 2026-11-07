# KRI-1237: phone Talking "show my screenshot when I talk about it" (failure modes)

Parent: KRI-1234 (Qendresa Q1 run, 2026-11-06, TestFlight 1910 / protocol 42, prod Fly v2122).

## The incident

Her ask (thread `b931bc4e`, turn 1, requirement r4): "When I talk about something, show the
matching screenshot or screen recording in the top or bottom part of the screen, not over my
face".

- Draft: `not_possible`, "This draft has no pop-ins timed to your words." Readiness
  `unsupported`, which alone held the first draft ("I can't make this yet").
- Cause: the ask went to the word-triggered pop-in check (`_check_reaction_beats`), which reads
  only the planner's `reaction_beats`. Talking cards come from a different step: the render grounds
  the Visuals pool on the words they match (`ground_phone_subtitled_overlays`). No draft fact
  modelled it, and no finished-video check read its `phone_overlay_receipt`.
- The render (job `e7d1e06f`, approved after "Make it without these" dropped r4) still placed all
  five screenshots; the five screen recordings were left out (`no_spoken_match`).

## Expected outcome (independent of the code)

Read from the prod records in `src/apps/api/tests/fixtures/kri1237_qendresa_q1_cards.json`:

- Draft: a promise ("I'll show your Visuals as cards when you talk about them; one you never talk
  about won't appear"), never `not_possible` / `unsupported`.
- Finished video: met; each screenshot named with the second it shows in the FINISHED video. The
  receipt keeps the original take's clock and speech cleanup removed 0-3.04 s (restart),
  12.965-14.334, 21.105-21.71 and 36.105-37.495 s, so:

  | Visual | receipt start (take) | removed before | finished video |
  | --- | --- | --- | --- |
  | Screenshot 1 spec-kit | 8.48 | 3.04 | 0:05 |
  | Screenshot 2 impeccable | 15.32 | 4.409 | 0:11 |
  | Screenshot 3 tradingage | 22.82 | 5.014 | 0:18 |
  | Screenshot 4 agents | 31.06 | 5.014 | 0:26 |
  | Screenshot 5 gamestudio | 39.32 | 6.404 | 0:33 |

- Each of the five screen recordings named as not shown.

## Ways the fix could fail (written before the fix)

1. The draft still answers `not_possible` / `unsupported` for her ask.
2. The draft promises cards the render will not place: overlay lane closed, no Visuals in the
   pool, only screen recordings while video cards are off, the full-screen layout, a non-phone
   edit, or several speaking clips (the worker skips the lanes).
3. The promise swallows a word-named pop-in ask ("when I say Spec Kit, pop up its sticker").
4. No Turkish twin, or not a format promise (a Turkish reply leaks English, or the draft asks
   "make it without these?" about something the render does).
5. After render, the take's seconds are quoted (0:08 instead of 0:05).
6. After render, a left-out Visual is not named, or a card the cut dropped is called shown.
7. After render, "met" with nothing placed, or a verdict read off a receipt about something else
   (hand-authored lane, failed matcher, full-screen sequence).
8. After render, raw upload names (iOS id prefix, a cut-off ".p").
9. With no overlay receipt, a card verdict is invented.

## Remaining gaps (not this change)

- (Closed here) Approvable: the promise is in KRI-1235's `RENDER_PROMISE_REASONS` (#2142), so with
  the prod switches of the run her turn-1 draft holds nothing (`test_q1_first_draft_is_not_held_with_prod_switches`).
- Position ("top or bottom, not over my face") is not judged here: the card layout belongs to
  KRI-1238 (item 15 layout); faces are avoided by the matcher's own face sampling.
- Which kind of picture an unnamed pop-in ask wants (a flag, an emoji) is not judged: KRI-667 bars
  reading it from words, so the brief writer would need a typed fact. The promise names the
  mechanism ("your Visuals ... when you talk about them"), and the receipt names what showed.
- A named word with no planner beat ("when I say X, show its screenshot") keeps the pop-in verdict
  even though the card matcher may still place it.
- Several speaking clips: no promise (the worker skips the lanes), so the old answer stays.
- Receipt labels are cut at 60 characters including the 37-character iOS id, so a name can arrive
  shortened ("Screen Recording 4 agen"); the worker's `_label_for_asset` is unchanged here.
