# Slide post replay fixture (synthetic)

Synthetic: no prod planner turn exists to record. The 7 prod slide jobs the fixture scout found
(2026-09-30 to 2026-10-06) were manual Plan-page posts: a `select_format` action and a title, no
planner turn, no strategy.

Request (hand-written): "Make a slide post of my Kas photos. Fast and punchy, start with the hook
'Three days, zero plans', go arrival, old town, sunset, title 'Three days in Kas' animated in,
kinetic captions, keep it 20 seconds, and add music."
Route: slide post (`_run_slide_post_job`), table `app/kria/route_capabilities/slide_post.py`.
Scorecard: `scripts/kria/score_route.py`.

    make kria-replay FIXTURE=slide_post
    cd src/apps/api && .venv/bin/python -m pytest tests/kria/test_route_replays.py -k slide_post

Offline and free: socket guard, no model, JSON only, no media.

## Recorded

Nothing.

## Reconstructed (everything)

- the creator message, the Creative Brief (r1 animate the title in, r2 keep it 20 seconds, r3
  kinetic captions) and seven photos (3 s each)
- the planner strategy: `slides` with `licensed_music`, `guided_story` direction, `fast` pace with
  `pacing_requested`, a hook idea, a three-beat story, `kinetic` captions, an opening title, `pop`
  title animation and a stated 20 s
- the approval card is assembled offline from the real pure functions; manifest and proxy paths are
  synthetic

## Checks

| Check | Kind | Foundation (cf4bd7575) | Lane |
| --- | --- | --- | --- |
| 1 route resolves to `slide_post` | route | VERIFIED | |
| 2.direction, 2.pacing, 2.intro_hook, 2.story_structure, 2.caption_style, 2.audio_strategy | disclosed_ok | VERIFIED (card lines) | |
| 2.title_animation | disclosed_ok | VERIFIED through the r1 decline (its own line is past the budget) | |
| 2.opening_title | disclosed_ok | NOT VERIFIED: the opening-title line falls past the card's 640-character budget | L6 |
| 2.target_duration_s (stated 20 s) | disclosed_ok | NOT VERIFIED: the length line falls past the card's 640-character budget | L6 |
| 3.r1 animate the title in, 3.r3 kinetic captions | disclosed_ok | VERIFIED (route declines) | |
| 3.r2 keep it 20 seconds | disclosed_ok | NOT VERIFIED: no receipt on a first draft and the length line is past the budget | L6 |
| 10 consumer gate | report | REPORT (gaps) | |

No honored checks: the slide lane (L6) discloses every video-only field and changes the planner
prompt, which an offline replay of a written strategy cannot show.

## What the checks do not prove

A synthetic plan shows what the card says for these choices, not that a planner would make them
(L6 stops it choosing direction, pace and music for slides). Card text within the 640-character
budget plus typed declines; no native render.
