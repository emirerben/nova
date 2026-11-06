# Qendresa "says done but isn't", group B (plan and render) — failure modes

Written BEFORE the code (project testing rule; plan `~/Downloads/kria-qendresa-test/handoff/
says_done_fixes.md`, cases 2, 4, 9 render half, 10). Every fix keys on typed brief facts
(KRI-667): no creator or model wording is read, no regex or word test is added. Group C owns the
receipt half of cases 2 and 9 (`brief_checks.py`); this group does not edit those checkers.

Each case has its own kill switch (bug fix: default `true`, `false` = the previous behaviour,
byte-identical). Set with `fly secrets set <NAME>=false --app nova-video` + api and worker restart.

| Case | Kill switch | What changes when on |
|---|---|---|
| 2 keep whole take | `KEEP_WHOLE_TAKE_PLAN_ENABLED` | approval-time length plan keeps the one video clip whole |
| 4 mute wins | `CREATOR_MUTE_WINS_ENABLED` | a whole-video `clip_sound: off` is never contradicted by a camera-audio pin |
| 9 whole-video text | `WHOLE_VIDEO_TEXT_PIN_ENABLED` | a global literal the planner drew as an opening title is pinned for the whole video |
| 10 title hold | `TITLE_HOLD_NOT_LENGTH_ENABLED` | a title-scoped timing row is the title's hold, never the video's length |

Tests: `tests/kria/test_says_done_b_keep_whole_take.py`,
`tests/tasks/test_says_done_b_mute_wins.py`, `tests/pipeline/test_says_done_b_whole_video_text.py`,
`tests/services/test_says_done_b_title_hold_length.py` (ported from the kit's `tests/bugs/`).

## Case 2 — "Keep the whole clip, don't cut it" (one-clip phone montage)

Typed ask: a live `timing` requirement, scope `global`, `facts.keep_whole_take: true`, no
`duration_s` of its own (a duration on the same row is a length ask, as `brief_checks` reads it).

| # | Failure mode | Handling / pinned by |
|---|---|---|
| 2.1 | Only the receipt reads the fact; the approval-time length plan keeps one best moment and the worker plays exactly that (11 s → 4 s). | `planner._enrich_best_parts_snapshot` passes the clip as a whole take to `build_length_plan`; its window is 0 → clip end. `test_keep_whole_take_plays_the_whole_clip` (kit port, red on main). |
| 2.2 | The pace / story ceiling (4 s holds) trims the whole take back down after selection. | The whole take's minimum is its full length, and `paced_selection` never sets a ceiling below a minimum. Same test (balanced pace, fast_montage). |
| 2.3 | The worker re-plans and drops it. | The worker plays the approved windows (`plan_unified_montage(approved_length_plan=…)`); the test plans through it, not the envelope alone. |
| 2.4 | Several video clips: "keep the whole clip" names no clip, and keeping every clip whole changes the montage into a different edit. | Applies only when the plan has exactly one video clip; otherwise unchanged (receipt half, group C, says it is cut). `test_two_clip_montage_is_unchanged`. |
| 2.5 | The creator also stated a length (or a creator duration is pinned). | The stated length decides, unchanged (the kit control: "Make it 11 seconds" keeps 0–11 s). `test_control_a_stated_length_keeps_the_whole_clip`. |
| 2.6 | A clip longer than the edit cap (120 s) or shorter than the 3 s minimum. | Not applied above the cap (no impossible-cap question); below 3 s the existing minimum conflict stands. |
| 2.7 | A moment ask, pinned named moments or a found closing line on the same take. | Those keep deciding (not applied). |
| 2.8 | A part of the clip the creator hid. | The hidden part still wins (`usable_s` caps the window). |
| 2.9 | Talking (subtitled), single-hero, voiceover- or song-owned lengths. | Not applied: only `edit_format: montage` whose length the footage owns. |
| 2.10 | Wording decides (the `_WHOLE_TAKE_RE` fallback). | Never read: only the typed fact. A brief without it is unchanged. |
| 2.11 | Switch off. | Plan byte-identical. `test_switch_off_keeps_the_old_plan`. |

## Case 4 — "Mute my sound" with `audio_strategy: original_audio`

Typed ask: live `audio` requirement(s) whose `clip_sound` asks (`clip_sound_toggle_ask`) contain a
whole-video `off` with no exceptions, and no `on` ask anywhere. "Mutes the whole video".

| # | Failure mode | Handling / pinned by |
|---|---|---|
| 4.1 | `build_render_contract` pins `original_audio="require"` from the strategy alone, so the muted recipe is refused and the draft's "Clip sound off" is broken. | With the brief muting the whole video, the contract pins no camera audio (`original_audio` stays `forbid` if set, `require` → none; `audio_source_ids` → none). `test_a_promised_mute_is_not_contradicted_by_the_render_contract` (kit port). |
| 4.2 | A job approved before the fix carries `require` in its pinned contract; the sound lane steps back to the unmuted plan. | The phone writer relaxes exactly those fields (the KRI-736 editor-Save relax) on a per-variant contract revision, re-stamps the length policy for the new digest, and the pin verifies against it. `test_whole_video_mute_survives_an_original_audio_strategy` (kit port). |
| 4.3 | Relaxing too much: a partial mute ("off except the last line", one clip, a window, a line) or any "on" ask still plays camera audio the pin may legitimately protect. | Not relaxed; unchanged. `test_partial_mute_keeps_the_pin`. |
| 4.4 | Relaxing when nothing conflicts writes a revision on every job. | Relaxed only when the lane's mute candidate actually conflicts on camera audio. Control: no contract → silent, no revision (`test_control_whole_video_mute_is_silent_without_a_contract`). |
| 4.5 | The length check silently disappears for the relaxed contract (policy stamped under the old digest). | `stamp_length_policy` is re-run for the relaxed digest with the old mode and bound, as the editor relax does. Asserted in the E2E test. |
| 4.6 | A voiceover edit loses its required voice. | `require_voiceover` is never touched. |
| 4.7 | Turkish ask behaves differently. | Typed facts only; language-free. |
| 4.9 | The relaxed revision changes the pinned contract's digest, and the initial render review (`initial_render_review_authority`) requires it to equal the prepared-execution stamp's `contract_digest`. | A job whose stamp records a contract digest is not relaxed (old step-down, honest "contract" reason). Such jobs were dispatched before this fix: dispatch now builds the contract with the brief (4.1), so it never pins camera audio under a whole-video mute. `test_a_lineage_stamped_contract_is_not_relaxed`. |
| 4.8 | Switch off. | Contract digest and lane byte-identical. `test_switch_off_keeps_the_pin`. |

## Case 9 (render half) — "put this text … for the whole video"

Typed ask: a live `text` requirement, scope `global`, with a `literal` (the brief rule records
"pinned in a corner or on screen for the whole video" this way).

| # | Failure mode | Handling / pinned by |
|---|---|---|
| 9.1 | The planner filed her line as `opening_title` (or wrote no text field, so `_title` used the brief literal): drawn 0–2.2 s of an 11 s video. | In `plan_unified_montage`, an opening title whose words are a live global literal becomes a whole-video pin (the shape the brief rule asked the planner for). `test_whole_video_line_stays_on_screen` (both filings) — the line covers 0 → end and the stored + rendered receipts say met. |
| 9.2 | A real opening title is turned into a pin, or a title ask loses the title that answered it. | Not when any live `title`-scope text ask exists (worded or wordless; the title gate already counts the planner's title as its words), the strategy has a typed title hold (`opening_title_duration_s`), or the title is animated (pins have no entrance). `test_title_literal_stays_a_title`, `test_a_wordless_title_ask_keeps_the_title`, `test_a_typed_title_hold_stays_a_title`. |
| 9.3 | The pinned contract's `opening` text role refuses the pin. | A pin from 0 s satisfies the `opening` role (`text_matches` accepts role-less rows; start ≤ one frame), in `verify_phone_recipe` and in the phone repair `_shows`. Checked by compiling the converted plan. |
| 9.4 | A route headline ("literal · A → B") or a chapter list. | Not the literal's words, so unchanged. |
| 9.5 | Same words already pinned. | `_title` already returns none (KRI-523); unchanged. |
| 9.6 | Which corner. | The row's typed `facts.position` when it is a pin corner (`middle_center` only where item 21's look is on); else `top_center`, the corner of the kit control. |
| 9.7 | A timed pin the planner filed as a title with no hold (the time lost). | Becomes whole-video: the brief has no typed time for it (KRI-667). Disclosed gap. |
| 9.8 | The kit test's own setup assert (`covered < total`) turns red once the render is fixed. | The kit asserts the scenario still occurs; the port asserts the stronger result (whole video AND met). Reported to the orchestrator. |
| 9.9 | Switch off. | Snapshot byte-identical. `test_switch_off_keeps_the_short_title`. |
| 9.10 | Neighbour (KRI-545): a global chapter-list literal that is NOT only the clip labels used to fall back to a 2.2 s opening title. | Now the same whole-video line (still on screen, still met; the contract's `any` role is satisfied). Behaviour change, called out for review. `tests/kria/test_chapter_list_title.py::test_a_literal_that_is_not_only_the_clip_labels_still_stands` (switch on and off). |

## Case 10 — "title … for the first 3 seconds" read as the video length

| # | Failure mode | Handling / pinned by |
|---|---|---|
| 10.1 | A `timing` row scoped to the title (`duration_s: 3`) is taken by `creator_duration` as the exact total: a ~80 s Talking edit "can't be made the length you asked". | `freeze_ask.times_one_moment` returns true for a title-scoped requirement: its seconds are the title's. Every length reader (`creator_duration`, render contract, planner target, receipts, duration questions) already goes through it. Kit `test_q1_title_hold_length[title_hold_scope_title]`. |
| 10.2 | The same hold filed as `timing/global` (`title_hold_scope_global`, `ending_hold`). | NOT fixed: its typed fields equal a real length ask's ("make it 3 seconds"); only the words differ, which KRI-667 forbids reading. Needs the brief writer to file a hold on its title / closing row (group E). Those two kit params stay red. |
| 10.3 | A real length ask stops counting. | Only scope `title` changes; `global` length rows unchanged. `test_a_global_length_still_counts`. |
| 10.4 | Switch off. | Unchanged. `test_switch_off_reads_the_title_row_as_before`. |
