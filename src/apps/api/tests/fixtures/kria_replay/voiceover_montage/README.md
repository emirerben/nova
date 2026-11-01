# Voiceover montage replay fixture (synthetic)

Synthetic: no prod thread exists to record. The fixture scout read the debug payload of the 19
newest `variants_ready` prod jobs (2026-10-27 to 2026-10-31); none had a recorded voiceover, and
the admin job list cannot filter on one.

Request (hand-written): "Cut my Kas weekend clips under the voiceover I recorded. Tell it as a
story: the drive in, the old town, the beach, then the sunset. Balanced pace, about 30 seconds,
kinetic captions, and animate the text."
Route: phone voiceover montage (`_run_phone_voiceover_montage_job`), table
`app/kria/route_capabilities/voiceover_montage.py`. Scorecard: `scripts/kria/score_route.py`.

    make kria-replay FIXTURE=voiceover_montage
    cd src/apps/api && .venv/bin/python -m pytest tests/kria/test_route_replays.py -k voiceover_montage

Offline and free: socket guard, no model, JSON only, no media.

## Recorded

Nothing.

## Reconstructed (everything)

- the creator message, the Creative Brief (r1 about 30 seconds, r2 animate the text, r3 kinetic
  captions) and six clips with durations and no analysis
- the planner strategy, written as the planner returns it for this request: montage with
  `audio_strategy: voiceover`, `guided_story` direction, `balanced` pace with the server's
  `pacing_requested` (the creator said it), a hook idea, a four-beat story, `kinetic` captions,
  `pop` title animation and a stated 30 s (`target_duration_requested`, `creator_duration_s`)
- the approval card is assembled offline from the real pure functions; manifest and proxy paths are
  synthetic

## Checks

| Check | Kind | Foundation (cf4bd7575) | Lane |
| --- | --- | --- | --- |
| 1 route resolves to `voiceover_montage` | route | VERIFIED | |
| 2.direction, 2.pacing, 2.intro_hook, 2.story_structure, 2.caption_style, 2.target_duration_s | disclosed_ok | VERIFIED (card lines) | |
| 2.title_animation | disclosed_ok | VERIFIED through the r2 decline (its own line is past the card's 640-character budget) | |
| 3.r1 about 30 seconds | disclosed_ok | VERIFIED (the card's length line) | |
| 3.r2 animate the text, 3.r3 kinetic captions | disclosed_ok | VERIFIED (route declines) | |
| 4.pacing, 4.target_duration_s, 4.title_animation, 5.r1, 5.r2 | honored | NOT VERIFIED (no render replay yet) | L1 |
| 10 consumer gate | report | REPORT (gaps) | |

Not graded: a skipped opening title ("no text", `omitted_copy_targets`). Its intro text conflicts
with this fixture's animation ask; the voiceover lane adds a fixture of its own if it honors it.

## What the checks do not prove

A synthetic plan shows what the card says for these choices, not that a planner would make them.
Card text within the 640-character budget plus typed declines; no native render. Honored checks
need `scripts/kria/route_replays/voiceover_montage.py`.
