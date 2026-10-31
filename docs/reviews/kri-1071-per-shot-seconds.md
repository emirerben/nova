# KRI-1071: one shot's "for N seconds" is never the video's length

Parent: KRI-829. Found while building the Leverkusen test kit (2026-10-30); not yet seen in a
live chat. Tested on main `d2fe95dc0` (live Fly `33722a0c9`).

## Original request (kit M5, verbatim)

> Pre-match montage in this order: the train, the warm-up, the crowd, the light show, the
> line-up sheet, the away fans. The warm-up clip has a private conversation on it, so mute it
> completely. Keep the sound on the train announcement and on the stadium announcer reading
> the line-up; mute the rest. Show the whole line-up sheet for 3 seconds so people can read
> the names, don't crop it. Make it 20 seconds.

## Acceptance

- The creator's length is 20 s everywhere it is recorded: the draft strategy
  (`creator_duration_s`, `target_duration_s`), the stamp written at approval dispatch, the
  render contract, and the planned montage.
- The line-up sheet's 3 s is that photo's hold only.
- A per-shot or per-moment "for N seconds" alone never becomes a creator length. The length
  then stays with the brief or the footage.
- A whole-video "make a video for 30 seconds" still plans the same 30 s as
  "Make it 30 seconds." when the brief records it.
- Still wrong after this fix (out of scope, see Gaps): a brief that files the shot hold as
  `timing/global` holds two lengths, and the planner reports a length conflict.

## Root cause

`recognize_total_duration_s` (`app/schemas/edit_proposal.py`) tried
`\b(?:for|lasting)\s+N\s*seconds` first, anywhere in the message, and returned on its first
match. So:

- "Show the whole line-up sheet for 3 seconds ... Make it 20 seconds." returned 3, in either
  sentence order.
- "Hold the photo for 2 seconds and make it 15 seconds." returned None (2 is out of range),
  hiding the 15.

Readers of that value:

- `agents/main_creator.py` (`creator_duration_s(creator_texts=...)`) stamps it as the
  creator's length and writes it over the model's `target_duration_s`. The runtime-v2 draft
  is saved from that strategy unchanged.
- `services/generative_jobs.py` re-reads the approved binding's request with the same reader
  at dispatch and stamps the job's `creator_strategy`. The cloud builder sizes from that stamp
  (`tasks/generative_build.py` `creator_target_duration_s`).
- Brief-first readers (`kria/planner.py` length plan, `creator_render_contract`) fall back to
  the stamp when the brief proves no single length.

## Fix

The reader no longer reads "for/lasting N seconds" at all. A first version narrowed the rule
with more word patterns, which the KRI-667 language-policy guard
(`tests/kria/test_kri667_no_new_intent_regexes.py`) rejects, and rightly so: the brief
already tells a hold (`timing/clip:<photo>`, a freeze, a moment) apart from the video's
length. "Make a video for 30 seconds" now reaches the plan only through the brief's
`timing/global` requirement, as "about 30 seconds" and "30 saniye olsun" already did.

## End to end on main vs this fix (offline)

The same chain runs in `src/apps/api/tests/kria/test_kri1071_per_shot_seconds.py`:

1. Main Creator post-processing
2. Length plan
3. Brief binding
4. Dispatch re-read
5. Render contract
6. Unified montage plan

The brief carries the recorded KRI-824 shape (`timing/clip:<photo>`).

| Case | Step | main | fix |
|---|---|---|---|
| M5 | draft `creator_duration_s` / `target_duration_s` | 3 / 3 | 20 / 20 |
| M5 | approval dispatch stamp | 3 | 20 |
| M5 | render contract / planned montage | 20 / 20 s | 20 / 20 s |
| M5 without "Make it 20 seconds." | draft `creator_duration_s` | 3 | none (model's 18) |
| M5 without "Make it 20 seconds." | length plan / contract | 3 / 3 | none / none |
| M5 without "Make it 20 seconds." | planned montage | 6.0 s | footage length |
| "Make a video for 30 seconds." (brief: global 30) | contract / planned montage | 30 / 30 s | 30 / 30 s |

## Phrase matrix (the reader alone)

| Phrasing | main | fix |
|---|---|---|
| "Show the line-up sheet for 3 seconds" | 3 | none |
| "Hold the title for 3 seconds" | 3 | none |
| "Keep the bus photo on screen for 4 seconds" | 4 | none |
| "Slow motion for 3 seconds on the jump" | 3 | none |
| "Freeze the frame on the goal for 3 seconds" | 3 | none |
| "Open with '1 CITY. 7 DAYS. 7 VIDEOS.' for 3 seconds" | 3 | none |
| "Hold the photo for 2 seconds and make it 15 seconds." | none | 15 |
| "Freeze the frame for 1 second on the bite. Make it 15 seconds." | none | 15 |
| "Make it 20 seconds. Show the sheet for 3 seconds." | 3 | 20 |
| "freeze for one second" (number as a word) | none | none |
| "Show the title for the first 5 seconds" / "in the first 5 seconds" | none | none |
| "Make a video for 30 seconds" / "A montage lasting 30 seconds" / "For 30 seconds." | 30 | none: the brief reads it |
| Turkish "Kadro kağıdını 3 saniye göster. Video 20 saniye olsun." | 20 | 20 |
| German "Zeig das Aufstellungsblatt 3 Sekunden lang" | none | none |

- Turkish per-shot seconds never matched the Turkish whole-video patterns, so nothing changes.
- German has no patterns; the brief reads it.

## Every kit prompt, before and after

All 61 `PROMPT.txt` files under `~/Downloads/kria-*/`: parser value and
`length_policy.creator_duration_s(creator_texts=[prompt])`.

| Prompt | parser main | parser fix | creator length main | creator length fix |
|---|---|---|---|---|
| cabin M2_long_takes_best_moments | 18 | 18 | 18 | 18 |
| julio M1_don_julio_lunch_montage | 15 | 15 | 15 | 15 |
| julio M2_one_take_five_moments | 12 | 12 | 12 | 12 |
| julio M3_three_things_missing_shot | 12 | 12 | 12 | 12 |
| julio2 M2_one_take_six_moments | 15 | 15 | 15 | 15 |
| leverkusen M1_match_day_in_filming_order_montage | 25 | 25 | 25 | 25 |
| leverkusen M2_taha_goal_reactions_montage | 12 | 12 | 12 | 12 |
| leverkusen M3_things_kria_cannot_know_montage | 15 | 15 | 15 | 15 |
| leverkusen M4_story_from_chat_photos_montage | 20 | 20 | 20 | 20 |
| **leverkusen M5_private_sound_and_lineup_sheet_montage** | 3 | **20** | 3 | **20** |
| stress M1_lisbon_precision_montage | 15 | 15 | 15 | 15 |
| stress M2_matcha_unsupported_asks | 20 | 20 | 20 | 20 |

The other 49 prompts read no length on main or with the fix. No kit prompt asks for the whole
video's length with "for N seconds".

## Gaps

- **Two lengths in the brief.** If the brief files the sheet's 3 s as `timing/global` (as
  prod filed a freeze in KRI-777), it holds two lengths and the planner raises "Your
  confirmed edit lengths conflict". That comes from how the brief is filed and from
  `freeze_ask.times_one_moment`, not from this reader.
- **Cloud builder.** A whole-video "for N seconds" no longer sets the cloud builder's
  `creator_target_duration_s`, which reads only the dispatch stamp. "About 30 seconds"
  already worked this way. The phone path, the render contract and the length plan read the
  brief.
- **The legacy web chat** (`routes/creator_agent.py`) also stops reading "for N seconds".
  The web editor is out of scope for Kria.
- **"Make it 3 seconds" about one shot.** In "the bus clip, make it 3 seconds", "it" is one
  shot, but this is still read as the whole-video length. The pronoun is ambiguous.
- **Turkish "X videosu".** "Otobüs videosu 3 saniye olsun" still reads as a 3 s video,
  because "X videosu" can name a source clip or the edit.
- **Not checked on a device.** Nothing has been checked on the iPhone or in prod yet. M5
  needs a re-send after deploy.
