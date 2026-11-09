# KRI-1327: Çiğdem S3, a Talking video made of three takes

Live run 2026-11-08 (TestFlight 1929, protocol 46, Fly 9d4ba22b5). Chats "Yorum1" (thread
`855274e8`) and "Yorum2" (thread `cc0e3706`, item `a55f08ab`, job `7416c94b`). Kit:
`kria-cigdem-test/S3_food_marketing_short` (three takes of one creator: A 20.1 s at home, B 7.0 s
at home, C 7.15 s in the car; four photos plus a decoy calendar).

## The request

> Yorumla episode 10: why brands do food marketing. There are 3 videos, two at home and one in
> the car, join them in that order. Cut the pauses, the "okay"s and the places I restarted. Show
> the Nar, Mira, donut and café pictures full screen when I mention them. In the last part in
> the car, put an orange line around me. Captions small, white, in the middle, never on my face.

## Acceptance (the kit's checklist, `S3_food_marketing_short/README.md`)

| # | Expected | Before (job 7416c94b) |
|---|---|---|
| 1 | Takes in order A → B → C, confirmed without a question | 4 holds before a draft ("couldn't tell which clip … means" for length, quoted words and place) |
| 2 | Dull start and the restart cut, or said up front that they can't be | Draft "Will do at render"; render ran cleanup `off_v1`; "no word timings" after |
| 3 | Pauses / length | 35.8 s, nothing cut |
| 4 | 4 photos full screen on their word, decoy never | Draft "Done" ×4; render `lanes_skipped: multiple_speakers`, 0/4 shown |
| 5 | Kria says before rendering it can't draw an orange line | "can't verify automatically" / "Have a look" |
| 6 | Captions small, middle, never on her face | Draft "Done: in the middle"; drawn at y 0.86 (measured 0.83) |
| F | Every blocker in one reply | One pin problem stamped on every ask; blockers surfaced one per turn |

## Root causes and fixes

| Bug | Root cause | Fix |
|---|---|---|
| A clip names | Talking plans no clip intents, so `build_render_contract` had no rows to seat a stated first/last take and refused every wording | `services/talking_clip_anchors.py` seats a take by length, quoted opening words or place from the snapshot (every cue must agree on one take); the Talking writer plays the pinned order (`_speakers_in_pinned_order`); readiness vouches for it only when every clip talks |
| F one blocker per turn | `approval_readiness` copied `contract.unresolved[0]` onto every row | `_order_problems` puts the pin lines on the order ask alone; every other ask keeps its own verdict |
| B photos | Several talking clips switched every lane off; grounding used the first take's words only | Behind `PHONE_TALKING_MULTI_SPEAKER_BEATS_ENABLED` (default off): full-screen photos are grounded on every take's words shifted to where the take plays; corner cards / closing shot are reported `multiple_speakers`. Draft and review say "won't show" whenever the lane is closed. Pop-in triggers also hear a plural / possessive "s" ("Nar" in "NARS", "café" in "cafes") |
| C cleanup | Several takes = no single narration source → contract `off_v1`, no choice offered; the draft still promised the cut | Honest format limit before approval (`_CLEANUP_SEVERAL_TAKES`); the worker records `several_takes`, not "no word timings". Real per-take cleanup stays KRI-720 |
| D captions | The face ladder jumped from the asked 0.5 straight to 0.86; the look receipt kept the stale 0.5 | The asked row is tried first (nearest, below on a tie); margin and look receipt record the drawn spot; a block outside the asked row is "Partly, lower so it stays off your face" |
| E outline | No fact for a line around a person, so nothing claimed it | Main Creator records `subject_outline: true`; `brief_checks` says "Couldn't" before approval and the draft goes ahead |

## Evidence (offline replay, real code, S3 data; no network, no model calls)

Tests written first from the S3 data; each file fails on origin/main 0e4ff67a3 and passes here:

- `tests/kria/test_s3_yorum2_talking_clip_order.py` (A, F, A3)
- `tests/tasks/test_s3_talking_pinned_take_order.py` (A2: attached B, A, C plays A, B, C)
- `tests/tasks/test_s3_three_takes_fullscreen_photos.py` (B: the real worker places all four photos at 11.5 / 13.6 / 25.9 / 29.7 s; decoy unused; kill switch off = today)
- `tests/kria/test_s3_multi_take_beats_receipt.py` (B: draft / review honesty)
- `tests/services/test_s3_trigger_heard_plural.py` (B: "NARS", "cafes")
- `tests/kria/test_s3_multi_take_cleanup_receipts.py` (C)
- `tests/pipeline/test_s3_caption_middle_off_face.py` (D)
- `tests/kria/test_s3_subject_outline_honesty.py` (E)

Command:

```
cd src/apps/api && .venv/bin/python -m pytest tests/kria/test_s3_*.py tests/tasks/test_s3_*.py \
  tests/services/test_s3_*.py tests/pipeline/test_s3_*.py
```

## Gaps

- Photos need `PHONE_TALKING_MULTI_SPEAKER_BEATS_ENABLED=true` (Fly, api + worker) and one device
  check: several speakers plus full-screen cards has never shipped (editor open / Save, preview).
- Cutting pauses and lines inside several takes is KRI-720 (consent per take, schema, iOS card).
- "donut" is never spoken in S3; the finished review says it was never heard. A draft-time
  "you never say this word" check is not built.
- Yorum1 (two takes attached as Visuals) now holds only the order ask; a "you said 3 videos but
  only 1 talks" message is not built.
- Turkish place words with endings ("arabadaki") are not matched yet; they get today's question.
