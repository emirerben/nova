# Narrated replay fixture (recorded, with reconstructed additions)

Prod thread (2026-10-26/27, 35 clips plus a recorded 150 s voiceover, `narrated_planned` item,
phone render on device). The creator's turns were chips: "Suggest an edit.", "Try again", "Use a
shorter voiceover", "Add a hook that matches the video", "Use this wording". The brief holds r1 (at
most 120 s) and r2 (the approved hook "Fenerbahçe matchday" as the opening title).
Route: phone Voiceover writer (`_run_phone_narrated_job`), table
`app/kria/route_capabilities/narrated.py`. Scorecard: `scripts/kria/score_route.py`.
Builder: `scripts/kria/build_route_fixture.py raw/ --route narrated` (reads `coverage.json`).

    make kria-replay FIXTURE=narrated
    cd src/apps/api && .venv/bin/python -m pytest tests/kria/test_route_replays.py -k narrated

Offline and free: socket guard, no model, JSON only, no media.

## Recorded (exact, from read-only prod GETs)

- the creator messages up to the approved draft, the planner's post-parse strategy and summary of
  the approved turn (the one that added the hook)
- Creative Brief v4 (r1, r2) bound to the job
- the 35 clip analyses, durations, audio
- the draft card prod showed and what the render did (`prod_outcome`)

## Reconstructed

- `strategy.title_animation = "typewriter"` and brief r3 (style, `facts.animation = "typewriter"`,
  "make the hook type in"): not in the recording; the Narrated lane's (L3) render target.
- the approval card is assembled offline from the real pure functions; manifest and proxy paths are
  synthetic

## Scrubbed

User, thread, item, job, turn and draft ids; storage paths; signed URLs; file names; hashes; media
ids (`analysis-proxy-narrated-NN`).

## Checks

| Check | Kind | Foundation | Lane |
| --- | --- | --- | --- |
| 1 route resolves to `narrated` | route | VERIFIED | |
| 2.direction (`native`) | disclosed_ok | VERIFIED (card line) | |
| 2.pacing (`balanced`) | disclosed_ok | VERIFIED (card line: every pace is named, the cuts follow the voiceover) | |
| 2.story_structure | disclosed_ok | VERIFIED (card line: the narrated table now lists it) | |
| 2.title_animation | disclosed_ok | VERIFIED (card line) | |
| 3.r1 at most 120 s | disclosed_ok | VERIFIED (the card's length line, which now fires from the creator's typed length ask as well as from the server's provenance) | |
| 3.r2 approved opening title | disclosed_ok | VERIFIED (checked met) | |
| 3.r3 animation ask | disclosed_ok | VERIFIED (route decline) | |
| 4.title_animation, 5.r3 | honored | NOT VERIFIED (no render replay yet) | L3 |
| 10.claims registry claims (every `Applied` field has a reader or a real carrier) | gate | VERIFIED | |
| 10 consumer gate | report | REPORT (gaps) | |

Not graded: `opening_title` (Applied in the table; check 10 holds the claim), `target_duration_s`
(graded through r1), `caption_style` auto.

## What prod did with it (recorded, informational)

The export ran 134.5 s (132.9 s edit plus a 1.6 s brand tail) against "at most 120 s"; the length
policy was `log_only`, so nothing enforced it, and the draft said only "I can't confirm this
draft's length yet". The stored title element reads "Come with us to a Fenerbahçe away day at Aston
Villa" in Montserrat with a typewriter entrance at a custom position. That is not the writer's
preset (Playfair Display, top band), and the variant carries an editor draft, so the creator most
likely edited it after the render: it is evidence neither that the writer dropped the approved
title nor that it drew it.

## What the checks do not prove

As for every route fixture: the field's own registry line (whole lines within the 640-character budget, then one compact line naming every line that did not fit) plus typed declines, no
wording quality, no native render. Honored checks need
`scripts/kria/route_replays/narrated.py`.
