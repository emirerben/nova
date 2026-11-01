# Talking head replay fixture (recorded, with reconstructed additions)

Prod request (2026-10-28, one horizontal clip, `subtitled` item, phone render on device):
"Talking video, filmed horizontally: make it vertical and keep his face in frame. ... When he first
says '1810', put a big '1810' on screen for a moment. When he says 'on the other side of the
square', show my Casa Rosada photo full screen for about two seconds ... Start at 'Okay, I'll
translate it to Turkish' and end after 'the government of the whole country'."
Route: phone Talking writer (`_run_phone_subtitled_job`), table
`app/kria/route_capabilities/talking_head.py`. Scorecard: `scripts/kria/score_route.py`.
Builder: `scripts/kria/build_route_fixture.py raw/ --route talking_head` (reads `coverage.json`).

    make kria-replay FIXTURE=talking_head      # summary + test-results/talking_head-replay/report.json
    cd src/apps/api && .venv/bin/python -m pytest tests/kria/test_route_replays.py -k talking_head

Offline and free: socket guard, no model, JSON only, no media.

## Recorded (exact, from read-only prod GETs)

- the creator message, the planner's post-parse strategy and summary (turn plan intents)
- Creative Brief v1 (r1 to r5) bound to the job
- the clip analysis (transcript, framing), duration, audio
- the draft card prod showed (`prod_outcome.approval_card`) and what the render did
  (`prod_outcome.render`)

## Reconstructed

- `strategy.title_animation = "pop"` and brief r6 (style, `facts.animation = "pop"`, "animate the
  big text in"): not in the recording. They give the fixture an animation ask, the Talking lane's
  (L2) render target.
- the approval card is assembled offline from the real pure functions the draft turn calls; the
  capability manifest and phone proxy paths are synthetic (`score_route.manifest`, `media_rows`)

## Scrubbed

User, thread, item, job, turn and draft ids; storage paths; signed URLs; the library photo id
(`asset-synthetic-01`); file names; content hashes; media id (`analysis-proxy-talking_head-01`).

## Checks

| Check | Kind | Foundation (cf4bd7575) | Lane |
| --- | --- | --- | --- |
| 1 route resolves to `talking_head` | route | VERIFIED | |
| 2.direction (`native`) | disclosed_ok | VERIFIED (card line) | |
| 2.pacing (`balanced`) | disclosed_ok | NOT VERIFIED: the card chip says "Balanced pacing", the writer follows the speech, and the table treats a default balanced as silence | L2 |
| 2.story_structure | disclosed_ok | VERIFIED (card line) | |
| 2.title_animation | disclosed_ok | VERIFIED (card line + r6 decline) | |
| 3.r6 animation ask | disclosed_ok | VERIFIED (route decline) | |
| 4.title_animation, 5.r6 | honored | NOT VERIFIED (no render replay yet) | L2 |
| 10 consumer gate | report | REPORT (gaps) | |

Not graded (each with its reason in `fixture.json` `checks.not_graded`): r1 start/end lines, r2
languages as spoken, r3 the "1810" text, r5 face in frame (all judged by the Talking writer's own
render receipts, outside the route registry); r4 the full-screen photo (offline the reconstructed
manifest has no reaction-beats capability or library photo, so the draft check declines it);
`caption_style` auto and `target_duration_s` 40 (nothing chosen or stated).

## What prod did with it (recorded, informational)

The draft said "Done" for the full-screen photo; the render left it unplaced (`never_heard`) and the
overlay agent put a different photo (Plaza de Mayo) for 5 s at "Buenos Aires'in". Every caption is
Turkish (`caption_language` tr), the end cut was `not_found` (the take ran to 40.8 s), and the
speaker framing fell back to letterbox (`no_face`). The "1810" text rendered at 18.975 s. These are
render-time gaps of the Talking writer, not planner fields; they are the reason r1 to r5 are listed
as not graded here rather than silently dropped.

## What the checks do not prove

A disclosure check reads the card text the creator sees (whole lines within the 640-character
budget, plus typed declines); it does not prove the line is the best wording. Honored checks need a
route replay module (`scripts/kria/route_replays/talking_head.py`) that runs the writer's planning
code on this fixture. The native iPhone render is not covered.
