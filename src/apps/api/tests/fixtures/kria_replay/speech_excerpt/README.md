# Spoken-excerpt montage replay fixture (synthetic)

Synthetic: no prod thread exists to record. The fixture scout pulled the debug payload of 48 recent
prod jobs (the 14 newest `variants_ready`, all 8 `awaiting_device`, `variants_ready` 15 to 40);
none carried a `speech_montage` trace.

Request (hand-written): "Make a montage of the best things I say in these coffee-tour clips. Cut
slowly between my lines, open with the hook 'Nobody tells you this about Turkish coffee', keep the
order roastery, sand pot, first sip, clean captions, animate the text, and put music under it."
Route: phone spoken-excerpt montage (`services/phone_speech_montage_job.py`), table
`app/kria/route_capabilities/speech_excerpt.py`. Scorecard: `scripts/kria/score_route.py`.

    make kria-replay FIXTURE=speech_excerpt
    cd src/apps/api && .venv/bin/python -m pytest tests/kria/test_route_replays.py -k speech_excerpt

Offline and free: socket guard, no model, JSON only, no media.

## Recorded

Nothing.

## Reconstructed (everything)

- the creator message, the Creative Brief (r1 animate the text, r2 clean captions) and six clips
- the planner strategy: montage with `licensed_music`, `voice_mode: excerpts` and
  `montage_audio` keeping the sound of three speaking clips (that is what routes a draft to the
  spoken-excerpt writer), `text_explainer` direction, `relaxed` pace with `pacing_requested`, a
  hook idea, a three-beat story, `clean` captions, `slide` title animation
- the approval card is assembled offline from the real pure functions; manifest and proxy paths are
  synthetic

## Checks

| Check | Kind | Foundation (cf4bd7575) | Lane |
| --- | --- | --- | --- |
| 1 route resolves to `speech_excerpt` | route | VERIFIED | |
| 2.direction, 2.pacing, 2.intro_hook, 2.story_structure, 2.caption_style | disclosed_ok | VERIFIED (card lines) | |
| 2.title_animation | disclosed_ok | VERIFIED through the r1 decline (its own line is past the budget) | |
| 2.audio_strategy (`licensed_music`) | disclosed_ok | NOT VERIFIED: the table's music line exists but falls past the card's 640-character budget behind five longer lines | L5 |
| 3.r1 animate the text, 3.r2 clean captions | disclosed_ok | VERIFIED (route declines) | |
| 4.pacing | honored | NOT VERIFIED (no render replay yet; today the cut is the fixed `_DEFAULT_CUT_S`) | L5 |
| 10 consumer gate | report | REPORT (gaps) | |

Not graded: `target_duration_s` (no stated length; the table marks it Applied and check 10 holds
the claim).

## What the checks do not prove

A synthetic plan shows what the card says for these choices, not that a planner would make them.
Card text within the 640-character budget plus typed declines; no native render. Honored checks
need `scripts/kria/route_replays/speech_excerpt.py`.
