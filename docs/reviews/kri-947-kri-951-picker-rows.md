# KRI-947 + KRI-951: one tap about a moment counts for every row about it

Parent incident: KRI-946 (Don Julio walk-in M2, prod thread 545F53A9, 2026-10-26, prod
`5a96e1d58`): no video in 7 rounds. This change fixes two of its six causes. Evidence level:
**offline replay** of the thread's real picker questions and answers; no device, live model
or production claim.

## Original request (kit M2 prompt, excerpt)

> Pull out six moments, about 2 seconds each, in this order: the DON JULIO window sign …,
> the queue under the striped awning, the butcher holding a cut of beef, … Right after the
> butcher moment, show the ribeye photo full screen with the text "Ojo de bife 🥩". …

## What the creator saw (prod) and what this change does

| Round | Prod question | After this change |
| --- | --- | --- |
| 3 | I couldn't find "the butcher holding a cut of beef", "the queue under the striped awning", "the butcher holding a cut of beef" in your video. (3 rows) | The queue is settled from the round-1 tap (KRI-947). I couldn't find "the butcher holding a cut of beef" in your video. Tap the video if it's in there, or tell me it isn't. (1 row) |
| 4 | I couldn't find "the ribeye photo", "the ribeye photo" in your video. (2 rows) | I couldn't find "the ribeye photo" in your video. Tap the video if it's in there, or tell me it isn't. (1 row; the caption row rides it) |
| 6 | I couldn't find "the ribeye photo" in your video. (asked again under `order` after the round-4 skip) | Not asked: the round-4 skip already answered the photo (KRI-947). |

Round 3's "after" sentence is produced by the real chain (planner rows replayed →
`plan_and_resolve_clip_intents` → resolver with no matches → picker → tap → next turn), not
by calling the wording function directly.

## Root causes

- KRI-947: `ClipSelections.match` carried an answer across ops only within
  `{group, include}`. The re-plan filed the tapped queue as `order:` (new intent id too), so
  no route matched.
- KRI-951: `build_clip_question` de-duplicated rows by key only, so `include:X` + `order:X`
  (or `+ caption:X`) were two rows with one label, and the question text joined every label.

## Change

- An answer settles the same attribute (exact, allowing for the 120-char key cut) or the same
  identity under any of `group` / `include` / `order` / `caption` (`_SAME_CLIPS_OPS`). Word
  overlap still only links `group` / `include`. `exclude` (KRI-667) and `label` (existing
  pin) never share.
- Rows about the same attribute in those ops that offer the same clips are one row; the
  others ride `also_answers` (the iPhone parser reads only key/label/op/candidates/
  suggestions, so no new build), and every answer, skip or "none" is recorded for all of them.
- The question names each moment once (case/space/punctuation-insensitive) and uses the
  one-moment wording when one is left; EN and TR.
- The resolver's row-count sanity check and the KRI-802 title-wording check read the joined
  rows too.

## Checks

`cd src/apps/api && pytest tests/services/test_kri947_kri951_m2_picker_rows.py` (fixture
`tests/fixtures/kri947_kri951_don_julio_walkin_m2_picker.json`, every picker event of the
thread, unchanged). Red before the change: the round-3 and round-6 re-asks, the duplicated
rows and names, the chain test. Wrong-content controls (green before and after): the queue
answer never settles another moment with overlapping words, an `exclude` or `label` row with
the same words, or a caption whose words overlap; rows with different clips, `exclude` or
`label` rows are never joined.

Mutation check (scratch, not committed): adding `exclude`/`label` to the shared ops,
letting word overlap cross ops, joining rows with different clips, or not recording the
hidden rows each turns at least one test red.

## Case matrix

- Covered: tap / "None of these" / "Skip, decide for me" on a joined row; a later re-plan
  that rewords the hidden caption row (identity still binds); EN + TR wording, one-video and
  several-clip wording; long descriptions cut at different places per op; rebuilding a
  joined question (the exclude merge path); title words on a hidden row (KRI-802).
- Not covered here (own tickets): the single take can't be settled by a tap (KRI-948); the
  photo is never a candidate (KRI-949); the caption dropped after a tap (KRI-950); vision
  error class not logged (KRI-952).

## Behaviour to know

"Skip, decide for me" already records "none" for every row it answers, which drops those
intents. Because the answer now also covers the same moment under another op, a skipped
moment is no longer re-asked in a new form; it stays out of the edit, as a same-op skip
already did.

## Remaining gaps

- iPhone card not checked on a device; the payload change is additive and the app parser
  ignores the new field (read in `src/apps/ios/Kria/Core/ClipSelection.swift`).
- M2 cannot reach a proposal until KRI-948 and KRI-949 also land; re-send M2 from the kit
  prompt in a new chat on the iPhone after they deploy.
