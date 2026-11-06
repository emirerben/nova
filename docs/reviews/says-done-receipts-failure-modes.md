# Qendresa "says done but isn't", group C (receipts) — failure modes

Written BEFORE the code (project testing rule). Parent: the ten Qendresa "says done but isn't"
cases (`~/Downloads/kria-qendresa-test/handoff/says_done_fixes.md`, outside the repo). Group C
makes Kria's words match the video for three of them, in `app/kria/brief_checks.py` plus the
facts plumbing it needs. Group B fixes the video itself for cases 2 and 9; every receipt below
must be right whether or not B's fix has landed.

| Case | Ask | Today | Wanted |
|---|---|---|---|
| 5 | "show the right recording when I talk about each one" (`speaker_background` + `background_changes`) | a recording whose phrase is never heard gets no window; the only trace is `phone_lane_receipt.dropped` `{window, reason: phrase_not_heard}`; the receipt says "3 photos or videos take turns behind you" (met) | not met, and the receipt names the recording that never shows |
| 9 | "this text for the whole video" (`text/global` literal) | filed as the opening title, drawn 0–2.2 s of 11 s; `_check_literal_text` checks only the words: met | judged by on-screen time: 2.2 s of 11 s is not met and says so; a line that really stays the whole video is met |
| 2 | "keep the whole clip, don't cut it" (`timing/global {keep_whole_take: true}`), one 11 s clip, montage | plan keeps 3.5–7.5 s; receipt "I can't confirm this draft keeps your whole take." (unchecked) | judged before approval: cut ⇒ "keeps 4 s of your 11 s clip" (not met); whole clip kept ⇒ met |

## Case 5 — a background recording that never shows

| # | Failure mode | Guard |
|---|---|---|
| 5.1 | **A `phrase_not_heard` note is not "never shown".** When not every entry has a phrase, or no phrase is heard at all, the layout falls back to a rotation and the unheard recording still gets a window (`test_unheard_phrase_falls_back_to_cadence_with_a_note`). Reading the note alone would blame a recording that plays. The receipt judges by the compiled windows: a recording is missing only when its photo / video is in no window. | `test_unheard_phrase_that_still_rotates_in_is_met` |
| 5.2 | **The same photo under two entries.** One entry unheard, the other heard: the photo still shows, so nothing is missing. Compare pool media ids, never sequence positions. | `test_unshown_entries_compare_photos_not_positions` |
| 5.3 | **A later step drops windows.** `drop_lane` on a `SubtitledLaneError` or the phone gate can strip the background after the layout. A missing list computed at layout time would go stale, so it is computed from the COMPILED lane (`lanes.background.windows`) when the lane receipt is written; a dropped lane is already `applied` without `background` ("none"). | worker test runs the real `_run_generative_job` |
| 5.4 | **Naming.** The receipt quotes the entry's `spoken_phrase` (the model's label for the recording, e.g. "chocolate fountain"); an entry without a phrase (or one dropped as `not_in_ready_pool`) is counted, not named. Never the creator's raw words; no regex (KRI-667). | `test_a_recording_that_never_shows_is_named` |
| 5.5 | **Old receipts.** A variant written before this change has no `unshown` key: the verdict stays what it was (no new claim either way). | `test_receipt_without_unshown_key_is_unchanged` |
| 5.6 | **Editor Save.** The variant's own `speaker_background` document wins over the first render's lane receipt; a stale `unshown` list from the first render must not be applied to the creator's own edit. | `_rendered_speaker_background_fact` returns before reading the lane receipt (unchanged branch) |
| 5.7 | **Turkish chat.** The new sentence has a Turkish twin through `say(en=, tr=)`. | `test_turkish_reason_names_the_recording` |
| 5.8 | **Lane receipt shape.** Existing assertions check `{lane, window, reason}` notes and exact `background` dicts. The drop notes are unchanged; `background.unshown` is added only when something is missing. | kit control `test_the_third_recording_really_never_shows` stays green |

Out of scope (named, not fixed): the draft before render cannot know which phrases will be heard,
so it keeps today's promise; a recording dropped for a reason other than an unheard phrase is
counted but not explained by reason.

## Case 9 — "for the whole video" text shown briefly

| # | Failure mode | Guard |
|---|---|---|
| 9.1 | **Not every `text/global` literal is whole-video text.** A spoken-word cue, an ending line (`closing`), a timed card, a list label, a researched explanation and a text sequence are global too but have their own time. Only a plain ask (no facts beyond look facts: position, font, colour, size, stack) is judged by on-screen time; every other ask keeps its verdict. Typed facts only, no wording (KRI-667). | `test_ending_line_is_not_judged_as_whole_video` |
| 9.2 | **A pin with a time the creator named.** "From 5 s to 10 s" is a pin with `start_s`/`end_s`/`clip`; it covers part of the video on purpose. A line placed by a timed pin keeps its verdict. | `test_timed_pin_keeps_its_verdict` |
| 9.3 | **The plan record does not know the title's hold.** `unified_montage.record()` keeps the title text, not how long it stays (decided by the guided compiler). The ask joins `judged_at_render`, so the render-ready review always judges it again on the finished rows (the record's words-only receipt is never what she reads). If group B's fix holds the line for the whole video, the finished rows say so and it is met; if B makes it a pin, the pin row covers the video and it is met. | kit param `filed_as_opening_title` + `no_text_field_brief_literal_becomes_title`; control `test_control_a_pinned_whole_video_line_is_done` |
| 9.3b | **A chapter list is global text too.** "Sabah · Üniversite · Gece" filed as `text/global` is met by each name on its own clips; it is never judged as one whole-video line. | `tests/kria/test_chapter_list_title.py` (unchanged, green) |
| 9.4 | **Coverage arithmetic.** Rows holding the line may overlap or abut (a pin split at a clip boundary). Coverage is the union of their spans against the finished length, with the same 0.05 s tolerance as `_wants_whole_text_span`; unknown length or no timed row holding the words ⇒ no time claim. | `test_line_split_across_two_rows_covering_the_video_is_met` |
| 9.5 | **Words split across rows** (stacked title, word-by-word sequence): no single row holds the line, so no time can be read; the words verdict stands. Named gap. | — |
| 9.6 | **Turkish chat.** The sentence has a Turkish twin. | `test_turkish_reason_for_brief_line` |
| 9.6b | **The guard goes blind once group B lands.** B turns the kit's two filings into a whole-video pin, so the kit test fails at its own setup and the param guard only proves "met". A guard must keep a line B leaves short: a typed title hold (`opening_title_duration_s`) and a same-words `text/title` ask. | `test_a_typed_short_title_hold_is_not_done`, `test_a_same_words_title_ask_keeps_the_line_short_and_says_so` |
| 9.7 | **Draft before approval** (strategy facts) and phone Talking variants carry no text times: unchanged. Named gap; the render is B's half. | — |

## Case 2 — "keep the whole clip" on a one-clip montage

| # | Failure mode | Guard |
|---|---|---|
| 2.0 | **Early windows are not the cut (review of #2129).** The length plan's `windows` and the record's `best_parts` are taken BEFORE the plan's later steps: a typed source-frame freeze (`_hold_at`) slides or shortens a window (11 s clip, freeze at 5 s: final cut 0–5 while `best_parts` says 0–11), `_reserve_freeze_hold` and the short-clip floor shorten, `_kept_out` slides, and a speed-up (`_ground_speeds`) can WIDEN it. So no claim (whole, cut, or an amount) is made from early windows: only the plan's final cuts (`final_source_cuts`, written into the record by the worker and the KRI-775 preflight from `snapshot.fast_cuts`) or the finished `story_timeline` are judged; with early windows only the receipt is the neutral "can't confirm". | `tests/tasks/test_keep_whole_freeze_review.py`; `test_the_length_plan_alone_never_claims_the_clip`, `test_a_record_without_final_cuts_claims_nothing` |
| 2.0b | **A stale stored verdict survives the render.** The render-ready review kept the record's stored keep-whole receipt (`judged_at_render` did not include it), so the finished timeline never corrected it. Keep-whole now joins `judged_at_render`: the review always re-judges it on the finished montage's cuts. The only readiness effect: an unconfirmed keep-whole draft is "I'll point it out after" instead of holding approval (editor drafts still hold); a judged "cut" still reaches her before approval. | `test_keep_whole_freeze_review.py` stale-record case |
| 2.1 | **Judged before approval.** The KRI-775 preflight runs the worker's own plan, so with its final cuts the creator hears "it will be cut" (and how much is kept) before she approves. The length plan alone (`with_planned_cuts`) never claims it (2.0). | `test_before_approval_the_preflight_plan_says_the_clip_is_cut` |
| 2.2 | **Whole clip kept ⇒ met.** Final cuts 0–11 s of an 11 s clip (e.g. with "make it 11 seconds") are met, at the worker and after render; never "cuts your footage down". | `test_whole_clip_kept_is_met`; freeze-review negative control |
| 2.3 | **Overlapping or split windows.** Kept seconds are the union of the clip's windows (two windows 0–5.5 and 5.5–11 keep it whole; two overlapping 3–7 windows keep 4 s, not 8). | `test_split_windows_covering_the_clip_are_met` |
| 2.4 | **Scope.** Only a one-footage-clip plan is judged this way. Multi-clip montages, Talking (keeps the take; speech-cleanup note), editor drafts and plans with no windows keep today's answers. | existing `tests/kria/test_brief_talking_receipts.py` |
| 2.5 | **Neutral vs judged.** The "cut" sentence is not a neutral reason, so `is_judged` is true and the draft holds for the creator's choice (`needs_creator_choice`); it is not a format limit. | `test_before_approval_the_preflight_plan_says_the_clip_is_cut` |
| 2.6 | **Turkish chat.** Turkish twin through `say`. | `test_turkish_reason_for_cut_take` |
| 2.6b | **Group B's plan keeps the clip; the receipt must follow the plan, not the format.** After B a one-clip footage montage with `keep_whole_take` plays 0 → end, and the format branch ("This montage edit cuts your footage down") would be false. The windows branch runs first; with B the final cuts and the finished timeline hold 0–11 s, so the worker and render-ready receipts are met. The kit's `test_q3_keep_whole_clip` reads `plan.record()` without final cuts, so on C alone it is red (no claim from early windows) and on B+C it is green (the plan plays 0–11). | `test_whole_clip_kept_is_met`; B+C merge check |
| 2.7 | **A stated length shorter than the clip.** `_wants_whole_take` already routes a keep-whole ask that carries its own `duration_s` to the length check; a separate shorter length ask makes the plan cut, and the keep-whole receipt then honestly says how much is kept. | covered by 2.1 shape |

## Switches

None. These change what Kria says about a video, not the video. The worker change adds one key to
`phone_lane_receipt.background` (`unshown`) only when an asked recording is missing.
