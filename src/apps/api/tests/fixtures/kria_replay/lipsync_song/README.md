# Lip-sync song replay fixture (recorded, with reconstructed additions)

Prod thread (2026-10-28, 4 takes plus a 271 s creator song, montage with `user_song` and
`song_sync: lipsync`, phone render on device): "Create a lipsync", then the song-range answer "I
filmed from 0:00 to 4:31". Brief r1: lip sync to the uploaded song.
Route: creator-song lip-sync montage (`pipeline/lipsync_montage.py`), table
`app/kria/route_capabilities/lipsync_song.py`. Scorecard: `scripts/kria/score_route.py`.
Builder: `scripts/kria/build_route_fixture.py raw/ --route lipsync_song` (reads `coverage.json`).

    make kria-replay FIXTURE=lipsync_song
    cd src/apps/api && .venv/bin/python -m pytest tests/kria/test_route_replays.py -k lipsync_song

Offline and free: socket guard, no model, JSON only, no media.

## Recorded (exact, from read-only prod GETs)

- the creator messages, the planner's post-parse strategy and summary
- Creative Brief v1 (r1) bound to the job
- the 4 take analyses, durations, audio
- the draft card prod showed and what the render did (`prod_outcome`)

## Reconstructed

- `strategy.caption_style = "karaoke"`, `strategy.title_animation = "pop"`, a stated 30 s length
  (`target_duration_s` 30 with `target_duration_requested` and `creator_duration_s`, as the server
  records a length the creator said), brief r2 (timing, `facts.duration_s = 30`, "keep it 30
  seconds") and r3 (style, `facts.animation = "pop"`, "animate the title in"). The recorded plan
  chose none of them; they give the fixture a caption style, a stated length and an animation ask
  the route drops. The animation ask is the lip-sync lane's (L4) render target.
- the approval card is assembled offline from the real pure functions; manifest and proxy paths are
  synthetic
- `replay_inputs` (lane L4): the song analysis and take alignment the render entry needs, which
  the recording does not hold. A 271 s song; a beat every 0.5 s and a lyric line every 4 s are
  synthetic. The takes sit in the song order prod placed them (`prod_outcome.render.song_windows_s`,
  matched by length: 04, 03, 02, 01), the first at the recorded window start 62.945 s, each
  overlapping the next by about half a second. No plan or ask value. The fixture builder does not
  carry this key: re-add it after a rebuild.

## Scrubbed

User, thread, item, job, turn and draft ids; storage paths (song and clips); signed URLs; file
names; hashes; media ids (`analysis-proxy-lipsync_song-NN`).

## Checks

| Check | Kind | Foundation | Lane |
| --- | --- | --- | --- |
| 1 route resolves to `lipsync_song` | route | VERIFIED | |
| 2.direction (`native`) | disclosed_ok | VERIFIED (card line: "it is cut to your song") | |
| 2.pacing (`balanced`) | disclosed_ok | VERIFIED (RecordedOnly line: "your song sets where the cuts fall") | |
| 2.story_structure | disclosed_ok | VERIFIED (card line) | |
| 2.caption_style (`karaoke`) | disclosed_ok | VERIFIED (card line) | |
| 2.target_duration_s (stated 30 s) | disclosed_ok | VERIFIED (length line) | |
| 2.title_animation | disclosed_ok | VERIFIED (card line + r3 decline) | L4: VERIFIED (same card line: no title to play it on) |
| 3.r2 keep it 30 seconds | disclosed_ok | VERIFIED (the card's length line) | |
| 3.r3 animation ask | disclosed_ok | VERIFIED (route decline) | L4: VERIFIED (same decline: no title to play it on) |
| 4.title_animation, 5.r3 | honored | NOT VERIFIED (no render replay yet) | L4: NOT VERIFIED, lane target kept (the entry stamps no entrance: this plan has no title) |
| 10.claims registry claims (every `Applied` field has a reader or a real carrier) | gate | VERIFIED | |
| 10 consumer gate | report | REPORT (gaps) | |

Not graded: r1 (lip sync is the route itself; check 1), the song section (see below).

## Song section

The scout read the creator's "0:00 to 4:31" as a section the render ignored (it used 62.9 to
123.8 s). It is the answer to "which part of the song did you film?": the range the aligner
searched, and the four takes were placed inside it. No typed song-section ask ("use the chorus")
exists in the brief or the strategy for a creator song, so a section this route ignores cannot be
graded offline until one does.

## What prod did with it (recorded, informational)

The plan said balanced and native; the recipe recorded pace `fast` and direction `fast_montage`
(60.888 s set by the song windows, planner target 25 s). Lyric captions were `available_hidden`
(51 words) and the typography agent chose Alte Haas Grotesk. The prod card said only "Have a look
at these in the video: lip sync to the uploaded song".

## What the checks do not prove

The field's own registry line (whole lines within the 640-character budget, then one compact line naming every line that did not fit) plus typed declines; no wording quality, no native
render. Honored checks come from `scripts/kria/route_replays/lipsync_song.py`, which calls the
route's `RENDER_ENTRY` (`plan_lipsync_montage`) with this fixture's own plan and asks. This plan has
no opening or closing title, so nothing could play "animate the title in": the entry stamps no
entrance, the card keeps the title-animation line and the r3 decline, and 4/5 stay lane targets.
With a title (or closing title) and a phone that plays text entrances, both titles enter the asked
way and the line and the decline go (agents/DECISIONS.md "Prompt fidelity wave 2: lipsync").
