# KRI-848: an honest song suggestion for a named genre

Parent: KRI-829 (Don Julio M1 case, KRI-802). Claim: **local fix, offline verified**.
No paid model call, no prompt change, no media change. Music stays recommend-only:
Kria names a track for the creator to add when posting and nothing is mixed into the
video. This change adds no length cap; the existing KRI-679 song-owned natural length
(`length_policy.py`, `length_planning.py`) is unchanged.

## Request and acceptance

Original request (Don Julio M1 `PROMPT.txt`, 2026-10-21, thread F4083F1D): "...Suggest
an upbeat tango song for it. Make it 15 seconds." It became brief requirement r8
(audio/global), joined by `_creator_music_ask` with the two keep-audible asks r6/r7.

Ticket Done-when: the M1 reply names the suggested song and calls it tango only if its
labels say tango; otherwise it says none was found.

Observable acceptance:

- Library holds a track whose labels say tango: the reply names it as the tango
  suggestion, even when the matcher would rank a non-tango track first or outside its
  four returned rows.
- Library holds none: the reply says "I don't have any tango tracks" and names the
  closest track with its artist, never calling it tango.
- A mood-only ask ("calm music", "upbeat song"), the keep-audible asks, a negated genre
  ("no rap, reggaeton or rock") and an everyday word that is not about music ("music for
  the house tour") change nothing: same pick, no new fields, no notice.
- English and Turkish replies; the notice survives when the receipts reply replaces the
  draft summary, and the 1000-character summary cap trims the model's text, not a notice.

## Root cause

`_freeze_licensed_music_section` passed the ask to `music_matcher` only as a soft prompt
signal. `MusicMatcherOutput.ranked` has `min_length=1`, so a track always comes back;
`MusicLabels.genre` is a 7-value enum that coerces "tango" to `other`; and Main Creator
writes the chat copy before the match, so the reply never named the song and could call
any pick tango.

## Boundaries traced

brief r8 → `_creator_music_ask` → `_gate_unresolved_choices` → `_prior_frozen_music` /
`_freeze_licensed_music_section` → `_match_best_track` → `_run_music_matcher` →
`MusicMatcherAgent` → frozen `selected_music_section` (draft + `length_context`) →
`kria_runtime` reply via `reply_from_receipts` (summary kept only when every judged ask
is met; otherwise receipts + `policy_notices`).

## Evidence (offline replay)

`src/apps/api/tests/kria/test_music_genre_honesty.py` drives the real gate, freeze,
`_match_best_track`, `_run_music_matcher` and matcher agent classes, then the real
`requirements_to_check_at_draft` → `build_receipts` → `reply_from_receipts`. Only
`MusicMatcherAgent.run` (a ranker that ignores the ask, like prod) and the catalog read
are canned; the catalog is in-memory tracks carrying `ai_labels`.

```sh
bash scripts/check-api.sh test tests/kria/test_music_genre_honesty.py \
  tests/kria/test_kri679_music_mood.py tests/agents/test_music_matcher.py \
  tests/evals/test_music_matcher_evals.py -q
```

- Branch (on main `1cfb929d5`): the command above plus `tests/gates/test_reliability_gates.py`
  gives 338 passed (genre file 177). Related music, choice-gate, length,
  generative-build and guided-story suites: 578 passed, 1 skipped (DB-only KRI-809 file
  without a scratch DB). Whole `tests/kria` on a scratch Postgres
  (`scripts/dev/test-db.sh`): 4805 passed, 6 skipped, 1 xfailed, 0 failed.
- Red control: the same test file copied onto unmodified main `1cfb929d5` gives **98
  failed, 79 passed**, every failure an honesty assertion (song never named, "tango"
  never said, the outranked tango track not suggested, no `asked_genre` field), no import
  or fixture error. The 79 passes are the intended controls (mood-only asks, negated
  genres, everyday and source-sound words that are not a song ask, a matcher already
  ranking the tango track first).
- Independent review (separate agent, three rounds): false genre asks ("music for the
  house tour", "Keep the tango band audible", "Make the music pop"), negations ("no rap,
  reggaeton or rock", "doesn't have to be tango"), over-claimed fits (enum family alone,
  "rock climbing" in a rationale) were found, turned into failing cases and fixed.
- Wrong-content control: a pop track ranked first while a tango track exists must not be
  suggested; a track whose title (not labels) says tango, a `genre: other` track, and a
  generic electronic track for "house" must not be called the asked genre.
- Free replay evals for `music_matcher` unchanged and green; the no-ask matcher prompt
  stays byte-identical (`test_no_ask_prompt_is_byte_identical`).

## Case matrix

| Case | Covered |
| --- | --- |
| No fitting track / fitting track outranked / fitting track ranked 5th+ | yes |
| Genre only in `vibe_tags`, `mood`, `ideal_content_profile` or classifier `rationale` | yes |
| Case, accents, hyphens ("electro-tango"), whole words ("rap" ≠ "therapy") | yes |
| Enum synonyms (hip hop, rap, EDM) vs narrow aliases (house, folk, orchestral) | yes |
| Everyday words only next to a music word; negated genres and genre lists | yes |
| Two genres in one ask; EN + TR copy; empty library; matcher failure | yes |
| Summary kept vs replaced by receipts; summary cap; reused and pre-fix sections | yes |
| iPhone export / production | no (see gaps) |

## Remaining gaps

- Not deployed; the M1 iPhone re-send in a fresh chat is owed after deploy (KRI-848).
- Whether the prod library holds any tango-labelled track was not checked offline. The
  song classifier is told to keep genre words out of `vibe_tags`; the check also reads
  `mood` and `rationale`, but a real tango track whose labels never say it is reported
  as "none found" (the safe direction). Better recall needs a classifier prompt change
  and a paid relabel.
- Render-time song matches (no chat ask), and chats without a Creative Brief, with a
  voiceover or with non-licensed audio, get no genre check.
- A song title in the ask ("like Tango in the Night") is read as a genre; the notice can
  be cut by `reply_from_receipts`' 1200-character cap on a very long receipts list (not
  the M1 path).
- The song's own label words are trusted: a `mood` such as "dance-floor drama" or
  "rock-solid" fits a dance or rock ask, and a non-everyday genre word in
  `ideal_content_profile` ("tango lessons") fits that genre.
- Reading the ask is word-based, not NLP. Every known miss leans to "not asked" (no
  filter, no notice: the pre-fix behaviour), e.g. "a song with a tango feel" or "Skip the
  blurry shot and suggest a tango song".
- The full Don Julio M1 request (KRI-802) stays open under KRI-829.
