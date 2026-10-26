# KRI-949: "the ribeye photo" asked as a moment "in your video" (Don Julio walk-in M2)

Offline verification of a bounded local fix. It does not establish fulfillment of the
whole M2 request or an iPhone run. Parent KRI-946 keeps those gaps: the queue moment
inside the one take (KRI-948), a picker answer lost when `include:` is re-planned as
`order:` (KRI-947), the caption dropped after a tap (KRI-950), duplicate labels in the
question (KRI-951) and the hidden vision error class (KRI-952).

## Request and failure

Kit `M2_one_take_six_moments` (Don Julio walk-in, 2026-10-26, iPhone protocol 22, prod
5a96e1d58, thread 545F53A9): one 137 s take as Footage and the ribeye photo as a Visual
(pool asset `d17aeec6`, `ready`). The prompt asks: "Right after the butcher moment, show
the ribeye photo full screen with the text "Ojo de bife 🥩"."

Stored turn plans (`admin.py --prod GET creation-threads/<id>/turns`), photo rows only:

| round | turn | photo intents (`intent_stats`) | reply |
| --- | --- | --- | --- |
| 1 | 6b961c32 | `include_ribeye_photo` n=1 0.80, `caption_ribeye_photo` n=1 0.80 | (asked about the queue) |
| 3 | 7baa1db5 | `order_ribeye_photo` n=1 0.80, `caption_ribeye_photo` n=1 0.80 | (asked about butcher/queue) |
| 4 | b80c1052 | `include_ribeye_photo` n=0, `caption_ribeye_photo` n=0 | "I couldn't find "the ribeye photo", "the ribeye photo" in your video." candidates = [video] |
| 6 | d8f1e9dd | `clip_order_4` ("the ribeye photo") n=0 | "I couldn't find "the ribeye photo" in your video." candidates = [video] |

`n` counts the resolver model's raw assignments, so n=0 with `nv=0 q=0` means the model
returned nothing at all for the photo in rounds 4 and 6. Why is not recoverable: the
planner's wording and the raw resolver output are not stored, and the resolver prompt
never says that a request naming "the … photo" refers to an image clip.

## Cause

1. The resolver returned nothing for the photo in two of the four rounds that asked for it.
2. KRI-779's one-video filter (`_live_photo_ids`, read by `_build_clip_question`) keeps a
   photo only when the analysis flagged it or its record is empty: "a still's record
   describes all of it", so an unflagged photo was taken as not showing the moment. When
   the resolver put nothing forward at all, that silence was read as a verdict on the
   photo. Only the video was left, so `clip_question_text` used the "in your video" copy
   and no answer could be right. KRI-693's photo row (merged) never had a photo to draw.

## Fix

`_live_photo_ids`: when the resolver put nothing forward for an intent
(`considered_media_ids` empty), every photo stays a candidate. The picker then offers the
video and the photos (KRI-693 thumbnail rows), and the copy is the "which of your clips"
question, never "in your video" with the photo left out. Nothing else changes: a moment
the resolver put the take forward for still settles on the take (KRI-779), a video-only
project keeps the plain "in your video" question, and the photo is never chosen for the
creator.

Not done, on purpose:

- **Using the only photo automatically.** Tried first; it can put the wrong photo in
  silently. KRI-692's Tigre fixture names "ışıklı ev fotoğrafı" with a bathtub as the only
  image, and the resolver returns nothing there too. The deciding signal would also have to
  read the creator's words, which the KRI-667 guard forbids
  (`tests/kria/test_kri667_no_new_intent_regexes.py`); a structured field from the planner
  would be the way.
- **Photo-only checks.** The yes/no vision check is video-only (`ClipQuestionAgent`,
  KRI-291), so an unsure photo cannot be re-checked by a model.

Remaining gap: if the resolver sends the take to a whole-take check for a photo request
and the check comes back unsure, KRI-779 still settles the photo request on the take.
Telling a photo request from a moment needs that structured planner field.

## Evidence (offline replay)

`src/apps/api/tests/services/test_kri949_named_photo.py`. Only the model transport is
faked (helpers shared with the KRI-779 test); the real resolver agent, intent resolution,
grounding and picker builder run. Resolver verdicts are rebuilt from each round's prod
`intent_stats`; non-photo attributes and the photo record are stand-ins (prod does not
expose them).

| | main fd44ab4e8 | this branch |
| --- | --- | --- |
| Round 4 replay | prod's question word for word, candidates [video] | both rows offer [video, photo] + photo thumbnail row, "which of your clips" |
| Round 6 replay | prod's question word for word, candidates [video] | [video, photo], "which of your clips" |
| Two photos | photos hidden | video + both photos |
| Turkish chat | "Videonda … bulamadım", photo hidden | photo offered ("Fotoğraf 1") |
| Video-only project | plain "in your video" question | unchanged |
| Moment the resolver put on the take, check said "no" (KRI-779 Merope) | settled on the take | unchanged |
| Exclude / vision still pending | no question / `pending` | unchanged |

Red control: on main the four incident tests fail (round 4, round 6, two photos, Turkish)
and the four controls pass; with the fix all eight pass. A scratch run with the same inputs
on main printed prod's two questions word for word with only the video as candidate.

Changed expectations: `test_a_moment_the_takes_record_rules_out_asks_plainly` and
`test_the_plain_question_follows_the_chat_language` (KRI-779) now run in a video-only
project. With the Cotopaxi photo present they pinned the behaviour this ticket fixes
(photo hidden when the resolver said nothing).
