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
- `replay_inputs` (lane L5): the grounded sections the speech planner would hand the compiler
  (montage 3.0 s with `cut_s` 0.8, line from clip 01, montage 4.5 s, line from clip 02, montage
  3.5 s, line from clip 03) and the three b-roll clips. The 0.8 is the value the speech planner
  prompt's own example writes; the request names no cut length. Synthetic portrait originals. No
  plan or ask value

## Checks

| Check | Kind | Foundation | Lane |
| --- | --- | --- | --- |
| 1 route resolves to `speech_excerpt` | route | VERIFIED | |
| 2.direction, 2.pacing, 2.intro_hook, 2.story_structure, 2.caption_style | disclosed_ok | VERIFIED (card lines) | 2.pacing: VERIFIED by the render (the pacing line is gone) |
| 2.title_animation | disclosed_ok | VERIFIED (named on the card's compact line, and the r1 decline) | VERIFIED (now a whole card line, and the r1 decline) |
| 2.audio_strategy (`licensed_music`) | disclosed_ok | VERIFIED (named on the card's compact line) | VERIFIED (now a whole card line) |
| 3.r1 animate the text, 3.r2 clean captions | disclosed_ok | VERIFIED (route declines) | |
| 4.pacing | honored | NOT VERIFIED (no render replay yet; today the cut is the fixed `_DEFAULT_CUT_S`) | VERIFIED (L5): relaxed holds 3.0, 2.25, 2.25, 3.5 s |
| 10.claims registry claims (every `Applied` field has a reader or a real carrier) | gate | VERIFIED | |
| 10 consumer gate | report | REPORT (gaps) | |

Not graded: `target_duration_s` (no stated length; the table marks it Applied and check 10 holds
the claim).

## What the checks do not prove

A synthetic plan shows what the card says for these choices, not that a planner would make them.
The field's own registry line (whole lines within the 640-character budget, then one compact line naming every line that did not fit) plus typed declines; no native render. Honored checks
need `scripts/kria/route_replays/speech_excerpt.py`, which calls the route's `RENDER_ENTRY` with
this fixture's own plan and asks. The layout has no cutaways (b-roll under a line holds 2.4 s
whatever the pace). A planner `cut_s` against the pace (the 0.8 under relaxed here) gives way to
the pace; one that goes the pace's way is kept. The b-roll here holds every paced cut whole; b-roll
too short for the pace is not replayed (the cut then runs as long as the clips allow, the video
keeps its length and the record says so).
