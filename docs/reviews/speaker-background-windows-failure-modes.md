# Commentary-style green screen: several backgrounds in time + the speaker small in a corner — failure modes (written before the code)

Linear: KRI-1162 (the feature), KRI-1161 (the chat reply that dropped the ask). Project
`Talk-to-camera green-screen backgrounds` (P-KRI-21). Extends the first cut's rows in
`docs/reviews/speaker-background-failure-modes.md` (S1–S26); the row numbers here continue (S27+).

The creator's verbatim ask (chat "Green screen", 2026-11-01): "Make the whole video like a green
screen: me small at the bottom right in front, and behind me full screen whichever photo or video I'm
talking about, changing every 3-5 seconds."

Scope of this cut: on a device-rendered Talk-to-camera (`subtitled`) edit, (a) the matted speaker
may be drawn scaled into a corner (`speaker_frame`), and (b) the background may be a timed
sequence of Visuals-pool photos / muted videos (`windows`), each shown for its window, with the
first-cut whole-edit background as the fill wherever no window covers the time. Entry points:
editor Background tool, chat (strategy + copilot ops). Out of scope: the live camera preview
(stays single-background), cloud variants, the web editor, blur, transitions other than a hard cut.

## Contract (the one source of truth for every lane)

### Recipe (V2 wire, `speaker_background`, snake_case; Swift decodes with `.convertFromSnakeCase`)

```
speaker_background: {
  asset_id, media_kind, source_start, loop, subject_track_id,      # unchanged first-cut fields = the FILL background
  windows?: [                                                       # ABSENT when unused (never [])
    {asset_id, media_kind: "image"|"video", source_start: 0.0, start: s, end: s}
  ],
  speaker_frame?: {width_fraction, x_fraction, y_fraction}          # ABSENT when unused (never null)
}
```

Rules, enforced identically by `app/kria/portable_speaker_background.py` and
`KriaMediaEngine/SpeakerBackground.swift`, pinned by `tests/fixtures/speaker-background-parity/v2.json`
(v1.json stays untouched and must still pass):

- `windows`: 1..40 entries, sorted by `start`, non-overlapping (`end_i <= start_{i+1}`), `0 <= start < end <= 1800`,
  `end - start >= 0.5`. Each entry names a `VisualRenderAsset` in the manifest whose `media_kind` matches.
  A photo has `source_start == 0`. A video window plays from `source_start` and loops inside its window.
  Where no window covers the time, the fill background (the top-level fields) shows.
- `speaker_frame`: `width_fraction` in `[0.2, 1.0]`, `x_fraction`/`y_fraction` in `[0, 1]` (the CENTRE of the
  scaled speaker clip frame, canvas fractions, same semantics as `VisualMediaPlacement.xFraction/yFraction`).
  The whole matted speaker layer (after its own crop/zoom) is scaled uniformly so its width is
  `width_fraction * canvas.width`, then centred at (x, y). Absent = full-frame (first-cut behaviour).
  Presets the chat and editor use (never raw numbers from the model):
  `full` = absent; `small_bottom_right` = {0.42, 0.76, 0.80}; `small_bottom_left` = {0.42, 0.24, 0.80};
  `small_top_right` = {0.42, 0.76, 0.22}; `small_top_left` = {0.42, 0.24, 0.22}.
- Unknown keys inside `speaker_background`, `windows[]`, `speaker_frame` reject the recipe (strict, as today).
- Capability: a recipe with `windows` or `speaker_frame` requires **`speakerBackgroundWindows`** (new token in
  `app/kria/recipes.py` and the Swift `MediaCapability` mirror; `tests/kria/test_capability_matrix.py`) in
  addition to `speakerBackground`. Both `EditRecipeV2` (Python) and `effectiveCapabilities` (Swift) derive it.
- A recipe with neither new field serialises byte-for-byte as today (digest invariant).

### Protocol and gates

- Client protocol **32** = decodes `windows` + `speaker_frame` + the `speakerBackgroundWindows` capability
  (`KriaClientProtocolContract.version` 28 → 32 (29 is left unused, 30 is the card-tilt build (#2033, KRI-1141) and 31 the media-backdrop build (#2038, KRI-1139)); `KriaTests`/`SuggestionChipsTests` header assertions move).
- Server: `speaker_background_windows_min_client_protocol: int = 32` (`app/config.py`). Gate
  `speaker_background_windows_supported(require_client=...)` in `app/services/phone_rollout.py` =
  `speaker_background_supported()` AND `speakerBackgroundWindows` in `phone_render_verified_features` AND
  (inside an app request) protocol >= 32. `speaker_background_windows_unavailable_reason()` names the
  first failing check: `disabled`, `token_not_verified`, `windows_token_not_verified`, `client_protocol_below_min`.
  No new env flag: `SPEAKER_BACKGROUND_ENABLED` stays the one kill switch; the new verified token is the
  device-parity gate for the new drawing.
- Strip order (`strip_unsupported_background` in `app/pipeline/phone_subtitled_lanes.py`): first the whole
  lane as today when the base gate is closed; else, when only the windows gate is closed, drop `windows` and
  `speaker_frame` (keep the fill background) and trace `speaker_background_windows_phone_fallback` with the
  reason. The result equals a request that asked for the single background only.
- `validate_phone_pilot_recipe` refuses a recipe carrying `windows`/`speaker_frame` without the token.
- Device-render poll and editor Save answer 426 `client_update_required` (`feature: "speaker_background_windows"`,
  `minimum_client_protocol: 32`) to a build below 32 for a pinned recipe / stored variant that carries them.

### Server lane (`app/pipeline/phone_subtitled_lanes.py`)

```
SubtitledBackground {
  media_id, gcs_path, generation, kind, source_start_s,            # unchanged = the fill
  windows: list[SubtitledBackgroundWindow] = []                    # omitted from every dump while empty
  speaker_frame: SubtitledSpeakerFrame | None = None               # omitted while None
}
SubtitledBackgroundWindow {media_id, gcs_path, generation, kind, source_start_s, start_s, end_s}
SubtitledSpeakerFrame {width_fraction, x_fraction, y_fraction}
```

Windows are main-timeline coordinates in the ORIGINAL take's time (like overlay cards), so
`remap_lanes_for_cut` remaps each with `remap_range`; a window entirely inside a removal is dropped
and its `media_id` returned with the dropped card ids so the caller can write a receipt. The fill is
untouched (whole-edit). Sorted/non-overlap is re-validated after the remap (touching windows may
meet exactly; that is fine).

### Editor document / Save

- Document `speaker_background`: `{media_id, kind, source_start_s, windows?: [{media_id, kind, source_start_s, start_s, end_s}], speaker_frame?: {width_fraction, x_fraction, y_fraction}}`.
  Keys absent while unused (existing documents are byte-identical).
- `EditorCommitSpeakerBackground` gains the same two optional fields. Every window's `media_id` must be a pinned
  pool visual (the route pins each new one before validation, `pin_pool_visual_for_background` per id);
  a window's video `source_start_s` must be inside that visual's duration. Refusal codes:
  `speaker_background_windows_disabled` (reason), `speaker_background_window_invalid` (order/overlap/length),
  `..._media_not_in_pool`, `..._kind_mismatch`, `..._start_out_of_range`, plus the 426 above.
- `editor_capabilities.speaker_background_windows: true` only while the windows gate is open on a variant that
  may carry a background.

### Chat

- Creator strategy `background_media` (reaction_beats.py `BackgroundMedia`) gains, all omitted while unused:
  `sequence: [{media_id, source_start_s?, spoken_phrase?, hold_s?}]` (max 40), `cadence_s` (2..10, default 4),
  `speaker_frame: "full"|"small_bottom_right"|"small_bottom_left"|"small_top_right"|"small_top_left"`.
  `media_id` = the fill when `sequence` is empty; with a sequence the fill is `media_id` (the model copies the
  first entry when the creator named no fill).
- Repair (`creator_capabilities._repair_creator_speaker_background`): every sequence id must be a photo/video
  of THIS item's pool; others dropped with a plain `Couldn't ...` notice; a sequence that loses every entry
  falls back to the fill alone; `speaker_frame` is kept only on a phone Talking manifest with the windows
  capability advertised (`capabilities.speaker_background_windows`), else dropped with a notice.
- Worker (`generative_build._resolve_strategy_background`): lays the sequence out on the KEPT take:
  an entry with `spoken_phrase` starts at the first caption-cue word match (loose text match) and ends at
  the next window's start or `+hold_s`/`cadence_s`; unanchored entries fill the remaining time in order,
  each `cadence_s` long, cycling through the list until the take ends (last window ends at the take end).
  Every resolved window is a `SubtitledBackgroundWindow`; an entry whose phrase is not heard is laid out as
  unanchored and gets a `phone_lane_receipt` note (`phrase_not_heard`). KRI-1175: a sequence whose
  entries ALL carry a phrase is laid out "on the word" instead (S46–S56), where an unheard one gets no window. Pool ids used by windows join
  `used_media_ids` (never also a card).
- Copilot ops (`editor_ops_v2/background.py`): `set_background_sequence {asset_ids: [...], cadence_s?}`
  (ordered pool ids, 1..40, replaces windows, keeps/sets the fill to the first), `set_speaker_frame {preset}`,
  `clear_speaker_frame`; `set_background` keeps its meaning (single fill, clears windows);
  `remove_background` clears everything. Snapshot `background` gains `windows` (ids + start/end) and
  `speaker_frame` while open; `available_windows: false` with a reason while only the windows gate is closed.
  Parity group `speaker_background` maps the new iOS session functions `setSpeakerBackgroundWindows`,
  `setSpeakerFrame`, `clearSpeakerFrame`.
- brief_checks (KRI-1161): a new ask detector `asks_speaker_background(req)` (EN/TR: "behind me", "green
  screen", "background photo/video", "arkamda", "arka plan" + photo/video words; never "background music")
  and a new fact group `speaker_background` on `PlanFacts` (open?, pool_count, fill id, window count,
  speaker_frame). Receipts: `met` ("N photos take turns behind you; you're small at the bottom right"),
  `partial` (fewer windows than asked / frame dropped), `not_possible` with a NAMED reason: no photo or
  video attached ("Attach the photos or videos first, in Visuals"), not an iPhone Talking edit, gate closed
  (`disabled`/token/protocol wording). The generic `_TALKING_NO_SUCH_EDIT` is never used for this ask.
  `approval_readiness` must list a checked `not_possible` of this kind as `unsupported` (named in the reply),
  never `unverified`.

## Decisions that remove whole classes of bugs

- **The fill stays; windows override.** Old decoders, old recipes and the camera pick keep working untouched;
  a cut that drops every window still has a background.
- **Windows on the take's original time, remapped at cut time.** Exactly how overlay cards already behave,
  so one remap function covers both; the fill never moves.
- **One matte, then one transform.** The person mask is computed on the full-size speaker layer exactly as
  today (S1–S7 unchanged); `speaker_frame` is a single affine transform applied to the CUT-OUT image before
  compositing. Nothing about segmentation quality or rate changes with the frame.
- **Hard cut between windows.** No crossfade in this cut: two decoded video backgrounds at once would double
  the decode cost (S14 budget) and a crossfade of cover-fit stills ghosts.
- **Presets, not geometry, from the model.** The chat and editor pick a named corner; only the compiler
  writes fractions.
- **The new token gates the new drawing.** `speakerBackground` alone (verified today) never lets a recipe
  with windows or a frame reach a phone; `speakerBackgroundWindows` is added to
  `PHONE_RENDER_VERIFIED_FEATURES` only after a physical iPhone export check.

| # | Failure mode | Treatment | Pinned by |
|---|---|---|---|
| S27 | Speaker drawn small but the matte mis-sized (mask computed after the shrink, haloed edge scaled) | Mask on the full-size speaker layer (as S1), cut out, THEN one uniform scale + translate to the frame; the mask never sees the shrunk image. | `SpeakerBackgroundCompositionTests.testSpeakerFramePutsTheCutOutInTheCorner` (speaker colour only inside the frame box; background colour at the old person centre) |
| S27b | The matte never produced a mask (S3's `.original` fallback) while a frame is set | The background is composed and the PLAIN speaker layer (room and all) is drawn small in the frame, so the creator sees their take in the corner rather than a full-frame room or a black canvas; with no frame the S3 behaviour is unchanged. | `SpeakerBackgroundCompositionTests.testSpeakerFramePutsTheCutOutInTheCorner` (frame path), existing S3 tests (no frame) |
| S45 | Editor timeline: a background window edited on the lane differs from what Save sends or what the render shows | One BACKGROUND lane row group in `makeRows` (shown only while the document has windows), each window a block of the shared timed-edit engine (select, body move, edge trim, snap, no overlap, 0.5 s minimum) writing `document.speakerBackground.windows` directly; the fill is a faint full-length strip (tap, no trim). Blocks carry client-local ids (`bg-<n>`, assigned at load, stripped on Save) so a drag keeps its block. A project without windows keeps its exact lane list and ids. | `BackgroundLaneUITests.testDraggingAWindowMovesTheInspectorAndTheSave`, `SpeakerBackgroundEditorTests.testWindowsCannotOverlapOrShrinkBelowHalfASecond`, `::testTimelineWithoutWindowsHasNoBackgroundLane` |
| S28 | Wrong background at a time (window order, gap, overlap) | Windows sorted and non-overlapping at decode on both sides; the compositor picks the single window containing `time` (`start <= t < end`), else the fill. | `SpeakerBackgroundTests.testWindowsMustBeSortedAndDisjoint`, `SpeakerBackgroundCompositionTests.testEachWindowShowsItsOwnBackground` (checker A at 1 s, B at 3 s, fill at 5 s), `tests/kria/test_speaker_background_windows.py::test_windows_sorted_and_disjoint` |
| S29 | Speech cut moves the take but not the windows (background shows during the wrong sentence) | Windows live on original take time and are remapped by `remap_lanes_for_cut` with `remap_range`; a window inside a removal is dropped with a receipt; the fill is untouched. | `tests/kria/test_speaker_background_windows.py::test_windows_follow_a_speech_cut`, `::test_a_window_inside_a_cut_is_dropped_with_a_receipt` |
| S30 | Video window shorter than its window, or `source_start` past its end | Loops inside the window from `source_start` (own composition track per window, muted, S11); compile fails the lane when no footage remains from `source_start`; a photo is held. | `SpeakerBackgroundTests.testShortVideoWindowLoopsInsideItsWindow`, `tests/kria/test_speaker_background_windows.py::test_a_video_window_without_footage_fails_the_lane` |
| S31 | Decode cost: many video windows decoded at once | Each video window is its own composition track inserted ONLY for its window; at most one background track has samples at any time, so per-frame cost equals the single-video case. Memory/time check on device owed before the token is verified. | `NativeCompositionTests.testBackgroundWindowsNeverOverlapInTheComposition`; device (owed) |
| S32 | Old app build receives `windows`/`speaker_frame` | Unknown keys below protocol 32 (strict decoder); the compile strips both below 32 or without the token; a pinned recipe answers 426 `speaker_background_windows` on poll and Save. | `tests/kria/test_speaker_background_windows.py::test_protocol_below_32_strips_windows_keeps_fill`, `tests/routes/test_speaker_background_windows_editor.py::test_poll_tells_an_old_build_to_update`, `::test_save_from_old_build_is_426` |
| S33 | Token not verified / kill switch off | Base gate closed: whole lane stripped (S17 unchanged). Windows token missing: `windows` + `speaker_frame` stripped, fill kept, trace `speaker_background_windows_phone_fallback`; `validate_phone_pilot_recipe` refuses an unstripped recipe. A recipe with neither field is byte-identical to today. | `tests/kria/test_speaker_background_windows.py::test_windows_token_missing_strips_windows_keeps_fill`, `::test_pilot_validation_refuses_unverified_windows`, `::test_no_windows_no_frame_is_byte_identical_to_today` |
| S34 | Python and Swift disagree on the new fields | Shared fixture `tests/fixtures/speaker-background-parity/v2.json` (valid → normalized, invalid rejected) read by both test suites; v1.json still passes on both. | `tests/kria/test_speaker_background_windows.py::test_shared_fixture_v2`, `SpeakerBackgroundTests.testSharedFixtureV2…` |
| S35 | A window names a visual outside this item's pool / another user's | Same pin as S19 per window id (`pin_pool_visual_for_background` for each new id before validation); the validator accepts only pinned ids; worker resolves only against the job's ready pool. | `tests/routes/test_speaker_background_windows_editor.py::test_a_window_from_outside_the_pool_is_refused`, `tests/tasks/test_speaker_background_strategy_worker.py::test_sequence_entries_outside_the_pool_are_dropped_with_receipts` |
| S36 | Chat: the ask is judged impossible and the reply hides it (KRI-1161) | Root cause: the draft turn judged Talking asks with `phone_talking` facts (checked `not_possible`) while `approval_readiness.assess_readiness` built its facts without them (`unverified`, approvable, hidden); readiness now builds the same facts, so a checked refusal is `unsupported` and named before "Make it without these". The ask itself is recognised through brief FACTS the planner writes (`facts.speaker_background`, `background_changes`, `speaker_small`, taught in `_BRIEF_PROMPT_SECTION` with the "background music is not this" exception), not new wording regexes (the KRI-667 guard `test_kri667_no_new_intent_regexes.py` forbids those); `_check_speaker_background` answers `met` / `partial` / `not_possible` with a named reason (no pool: "Attach the photos or videos first, in Visuals"; not an iPhone Talking edit; gate closed; app too old) and runs before the generic Talking refusal. Limit: a brief written before this change is not recognised until the planner restates it. | `tests/kria/test_brief_checks_speaker_background.py` (31 tests, incl. `test_readiness_lists_a_refused_talking_ask_as_unsupported_not_unverified`, the no-pool ask, background-music exclusion) |
| S37 | Chat: the model emits geometry or a corner the phone cannot draw | Presets only (schema `Literal`); repair drops `speaker_frame` with a notice when the windows capability is not advertised. | `tests/kria/test_speaker_background_chat.py::test_speaker_frame_preset_is_dropped_when_windows_are_closed` |
| S38 | Chat: "whichever photo I'm talking about" anchored to a phrase that is never said | The entry is laid out as unanchored (cadence) with a `phrase_not_heard` receipt note; never a failed job, never a silent drop. On an all-phrased sequence (KRI-1175, S52) it gets no window instead, with the same note. | `tests/tasks/test_speaker_background_strategy_worker.py::test_unheard_phrase_falls_back_to_cadence_with_a_note` |
| S39 | Chat: a sequence longer than the take, or a take longer than the sequence | Windows past the take end are cut at the take end (dropped when nothing remains). **Superseded by S57 (2026-11-05):** a shorter sequence no longer cycles; each entry shows once, the cadence stretching to cover the take. | `tests/tasks/test_speaker_background_strategy_worker.py::test_a_short_sequence_shows_each_photo_once_to_the_take_end`, `::test_windows_past_the_take_end_are_clipped` |
| S40 | Editor parity drift: new iOS session functions without agent ops | Parity group `speaker_background` gains `setSpeakerBackgroundWindows`, `setSpeakerFrame`, `clearSpeakerFrame`. | `test_ios_editor_actions_have_agent_ops` |
| S41 | Capability drift | `speakerBackgroundWindows` in both `MediaCapability` mirrors and the capability-matrix doc. | `tests/kria/test_capability_matrix.py` |
| S42 | Protocol collision | Server's newest is 28 (`media_tilt_min_client_protocol`), app `version = 28`; the open card-tilt PR #2033 (KRI-1141) takes 30, #2038 (KRI-1139) takes 31 and 29 is unused, so this feature takes **32**. Re-check open branches before merge. | `KriaTests` header assertions |
| S43 | The fill asset is also a window asset, or a window asset is also a card | Allowed for fill == window (same photo may fill and open); window/fill ids are reserved from `used_media_ids` so they are never also a cutaway or card. | `tests/tasks/test_speaker_background_strategy_worker.py::test_window_ids_are_reserved_from_cards` |
| S44 | Live camera preview shows one background while the plan will switch | The camera pick stays single (the fill). The teleprompter shows "Preview" and never claims parity (S24); nothing new to draw there in this cut. | n/a, copy review |
| S45 | The editor's live preview (and its Download/Share copy) of a fill + windows + corner-speaker edit draws the room with a full-size speaker while the phone export is right (KRI-1179, Green 3 on TestFlight 1876) | Same cause as S40 in the first-cut doc: the editor compiler named the speaker track "video", so no clip was cut out and neither the windows nor `speakerFrame` were drawn. Fixed by #2084 (the track takes the layer's `subjectTrackID`). The device re-render after an editor Save is not affected: it runs the server-pinned recipe, whose speaker track is "subtitled". | `SpeakerBackgroundLivePreviewTests.testTheEditorPreviewOfACornerSpeakerWithTimedBackgroundsShowsEachBackground`, `testExportingTheEditorPreviewOfACornerSpeakerWithTimedBackgroundsDrawsThem` |

## KRI-1175: "whichever photo or video I'm talking about" (written before the code)

Prod run "Green 3" (2026-11-03, job `9bad2d75`): the planner listed the five visuals in filename
order with no `spoken_phrase` (the prompt said to use one only when the creator pairs a photo with
words), so `lay_out` rotated them every 4 s and repeated them (8 windows). Replaying the same words
and cut with the right phrases showed the layout itself also fails the ask: a visual without an
anchor is dealt into every gap (the drone video appeared at 0 s, before the drone photo), the fill
flashes in sub-0.5 s gaps and repeats the first photo while others are still to come, two visuals
on one mention drop the first, and the 15 s after the last mention hold one visual. Root cause and
the design decision: `.dev/root-cause.md` of the KRI-1175 branch, summarised in the PR.

**Mode is decided by the request.** A sequence where EVERY entry carries a `spoken_phrase` is laid
out "on the word" (rules below). A sequence with no phrase keeps the rotation byte-for-byte; a
partly phrased one keeps the mixed layout above (S38/S39 unchanged there). Intent is never read off
the creator's wording on the server (`test_kri667_no_new_intent_regexes.py`): the planner is taught
to phrase every entry for a "whichever I'm talking about" ask (Main Creator v114).

| # | Failure mode | Treatment | Pinned by |
|---|---|---|---|
| S46 | The planner reads "whichever photo I'm talking about" as a rotation and writes no phrase (the Green 3 bug) | The windows section teaches: for that ask, every entry gets the `spoken_phrase` copied from the take's own words naming what it shows, entries in spoken order, two visuals of one thing share the phrase, a visual never talked about is left out and named in `summary`. Model behaviour on the Green 3 input is NOT proven offline (paid replay, not run). | `tests/kria/test_speaker_background_chat.py` (the section teaches it, shown only with the windows capability) |
| S47 | A visual starts away from its word (source vs kept-take time, the speech cut shifts it) | Unchanged anchoring: first kept-take hearing of the phrase, laid out in output time, written back in original take time. | `tests/tasks/test_speaker_background_on_the_word.py::test_green3_each_visual_starts_on_its_word` (real words + real cut, ±1.5 s in both time bases) |
| S48 | Two visuals of one mention: the second shows first, or the first is dropped (`window_too_short`) | Entries sharing one hearing play one after another in list order, one cadence each; when the next mention leaves too little room they share it evenly, and only entries that cannot get 0.5 s are dropped with `window_too_short`. | `::test_green3_each_visual_starts_on_its_word` (drone video right after the drone photo), `::test_entries_sharing_a_mention_that_cannot_all_fit_drop_the_last_with_a_note` |
| S49 | Between mentions the fill shows (repeating a visual already seen) or flashes in a sub-0.5 s gap while visuals are still to come | Each window holds until the next mention; no visual ever repeats (S57); an explicit `hold_s` still ends a window early when the fill is a separate base, and gives way to no-repeat when the fill is one of the sequence's own photos (S60). | `::test_green3_nothing_repeats_before_every_visual_was_shown`, `::test_an_explicit_hold_still_ends_the_window_early`, `tests/tasks/test_background_no_repeat.py::test_a_hold_gives_way_when_the_fill_is_one_of_the_photos` |
| S50 | A lead-in before the first mention shorter than 0.5 s flashes the fill | Absorbed: the first window starts at 0. A longer lead-in shows the fill. | `::test_a_lead_in_shorter_than_half_a_second_is_absorbed` |
| S51 | After the last mention one visual is held for the rest of the take ("changing every 3-5 seconds" broken) | **Reversed 2026-11-05 (S57, Yasin + Emir after S4 "fb 1"):** the shown visuals no longer take turns again; the last visual holds to the take end (a longer scene beats a repeat). | `::test_green3_the_last_photo_holds_to_the_end`, `::test_a_long_tail_is_one_window` |
| S52 | A phrase that is never heard is cycled into the talk (a photo nobody talks about shows) | On the word, an unheard entry gets no window and keeps its `phrase_not_heard` note (never a failed job). | `::test_a_phrase_never_heard_is_not_cycled_in` (Green 3 with the lunch decoy wrongly phrased) |
| S53 | The decoy (not in the sequence) appears anyway, as the fill, a window or a card | Only sequence entries and the fill can be backgrounds; the decoy goes to the card matcher, which finds no spoken match. | `::test_green3_worker_seats_the_backgrounds_on_their_words` (whole recipe searched) |
| S54 | The new layout changes a rotation or a mixed request | Only an all-phrased sequence takes the on-the-word path. A rotation / mixed request keeps its own path but follows S57 (each photo once). | `::test_green3_stored_strategy_shows_each_photo_once` + every S38/S39 test above |
| S55 | Windows break the phone contract (overlap, under 0.5 s, past the take, over 40) | Every window still passes `SpeakerBackgroundLayer`/`SpeakerBackground.swift` (shared fixtures untouched). | `::test_green3_worker_seats_the_backgrounds_on_their_words` (recipe built and validated), `tests/kria/test_speaker_background_windows.py::test_shared_fixture_v2` |
| S56 | The word check accepts the wrong photo on a word (no negative control) | The check compares each visual with the kit's ground-truth word, never with the phrase it was given. | `::test_the_word_check_rejects_swapped_photos` |

Device tests owed before `speakerBackgroundWindows` is verified: on an iPhone, a Talk-to-camera take with
three photo windows and the speaker small bottom-right must show each photo in its window and the creator
cleanly cut out in the corner in preview and export; a video window must loop inside its window and be
silent; a 60 s export with ten windows must finish within 120 s without a thermal step-down.

## No repeats (S4 "fb 1", decided 2026-11-05)

S4 run "fb 1" (prod 2026-11-04, job `c39a3210`): 8 photos over 19 windows, every photo shown 2–3 times (S51's tail
cycling after the planner put every mention in the first 29 s). Decision (Yasin + Emir): photos stay in a row, each
one stays until the next is mentioned, a longer scene is fine, nothing plain in between, no question to the creator,
and **the same photo never comes up twice**. Incident record: `docs/reviews/background-no-repeat-failure-modes.md`.
`lay_out` now takes each entry's `media_key` and the fill's `fill_key` (both callers pass them).

| # | Failure | Guard | Test |
|---|---|---|---|
| S57 | A photo is shown twice (tail cycling, rotation cycling, the fill repeating a photo) | Every distinct photo (by `media_key`) gets at most one window and the fill never shows a photo after its own window. On the word the last window holds to the take end; a rotation shows each entry once, its cadence stretched to `max(cadence_s, take / entries, take / 40)` (note `cadence_stretched`); a mixed request fills the gaps with the unphrased entries once each, then the window before a gap holds through it. | `tests/tasks/test_background_no_repeat.py::test_fb1_shaped_plan_never_shows_a_picture_twice`, `::test_rotation_without_phrases_shows_each_picture_once`, `::test_mixed_request_shows_each_photo_once` |
| S58 | The opening (before the first mention) shows nothing or a photo that comes back later | The first window starts at 0 s. Only a held first window with a fill that is a separate base (a colour, or a photo outside the sequence) keeps that base before it (S50). | `::test_the_opening_is_the_first_photo` |
| S59 | The planner lists one photo twice (two mentions, one `media_id`) | The entry heard first keeps its window; the other gets none and a `duplicate_media` note; the window before its mention holds through. | `::test_the_same_picture_listed_twice_is_shown_once` |
| S60 | An explicit `hold_s` opens a gap the fill covers, and the fill is one of the sequence's photos | No repeat wins: the held window stays until the next window. A fill outside the sequence (a colour, a separate base photo) still shows in that gap. | `::test_a_hold_gives_way_when_the_fill_is_one_of_the_photos`, `::test_a_separate_base_still_shows_after_a_hold` |
| S61 | A phrase never heard, or two photos on one mention | Unchanged: an unheard entry gets no window (`phrase_not_heard`, never cycled in); photos sharing a mention play back to back and share the span when it is short. Neither ever shows twice. | `::test_a_phrase_never_heard_is_skipped`, `::test_two_photos_on_one_mention_share_it` |
| S62 | Phone limits: over 40 windows, under 0.5 s, past the take, gaps that flash | At most one window per entry (entries are ≤ 40), each ≥ 0.5 s, the last ends at the take end, and every second of the take is covered by a window or (only where S58/S60 allow) the fill. | every test above (`_assert_each_picture_once` checks coverage) |
| S63 | The receipt promises "every 3–5 s" while a photo now stays longer | The receipt never names a pace: "N photos or videos take turns behind you". Gap: "Yapıldı" still sits beside the creator's own "every 3–5 seconds" wording in the requirement title (the brief has no pace fact). | `tests/kria/test_brief_checks_speaker_background.py` (unchanged wording) |

## Qendresa item 17: names heard when said another way (written before the code)

Flag `BACKGROUND_FOLLOWS_TALK_ENABLED` (default off, server only). Test kit run Q2 (2026-11-04,
`kria-qendresa-test/_src/kria_check/Q2/REPORT.md` row B5): a screen walkthrough of five repos, each
recording phrased on its name. Two problems: after the last mention earlier recordings cycled back
(S51), and her phrases as typed missed: "Claude code game studio" is spoken "studios" and
"TradingAgents" is spoken "trading agents" (both `phrase_not_heard`, the recording never shows), and
"agents" anchored inside "trading agents" (recording 3 cut to 4 s).

The cycling is fixed for everyone by the no-repeat decision above (S57: the last window holds to the
take end). Item 17 first kept S51 for a creator who names a rhythm (`background_media.keep_changing`);
that would show photos twice, so it was dropped when #2118 landed. What remains is anchoring: with the
flag on, every phrased entry is also heard when said another way.

| # | Failure mode | Treatment | Pinned by |
|---|---|---|---|
| S64 | Flag off changes anything | Off: `_anchor_time` runs unchanged and the prompt has no new bytes. Not inert: the Main Creator prompt version label moves to v123. | `tests/tasks/test_background_follows_talk.py::test_flag_off_windows_and_notes_are_byte_identical` (frozen), `tests/kria/test_speaker_background_chat.py` prompt bytes with the flag off |
| S65 | A name spoken another way is never heard: a final plural "s" ("studio"/"studios"), joined or split words ("TradingAgents"/"trading agents") | After the exact and KRI-961 matchers hear nothing, a loose name match compares the words with spaces removed and a final "s" ignored on words of five or more letters (not "-ss", "-us", "-is"). Then, for names of two or more words only, the exact and KRI-961 matchers run again with the last word's plural toggled, so a sound-alike plus a plural is heard (her real walkthrough's transcript says "cloud code game studios" for "Claude code game studio"). A sound-alike KRI-961 does not know ("Clawed") stays unheard. | `::test_her_label_spellings_are_heard`, `::test_a_sound_alike_with_a_plural_is_still_heard` |
| S66 | The loose match hears a different word ("island" in "this is land", "Notion" in "some notions", "Canva" in "canvas") | Last resort only (never when the exact or KRI-961 matcher hears the phrase); words are aligned one to one, or one word to two or three joined words of three letters or more; a single word against a single word is never loose; a name under four letters never matches loosely. | `::test_the_loose_match_never_hears_a_different_word` (negative controls) |
| S67 | A short name inside another visual's longer name ("agents" in "trading agents") anchors there and cuts that visual short | A hearing that lies inside another entry's hearing of a longer phrase belongs to that entry and is skipped; the next hearing is used. Two visuals with the same phrase still share their mention. | `::test_a_name_inside_another_visuals_name_is_not_its_word`, S48 tests unchanged |
| S68 | Two visuals of one thing get nested phrases ("drones" / "mounted on drones") and the shorter is never said alone, so the inside rule (S67) drops it | A hearing inside another entry's phrase is skipped only when the entry has a free hearing in the kept take; otherwise its first hearing anchors, as with the flag off. | `::test_a_name_that_only_occurs_inside_another_name_still_shows` (Green 3 words) |
| S69 | A common word ("agents") is said again inside the previous topic, so the visual still starts early | Not solvable from the words alone. v123 teaches the planner to copy the words said only where the visual is introduced ("number four agents"). Model behaviour not proven offline (paid replay, not run). | `tests/kria/test_speaker_background_chat.py` (the paragraph teaches it, only with the flag on) |

## Qendresa item 16: she starts full screen in her own room, then drops into the corner (written before the code)

Flag `SPEAKER_FULL_OPENING_ENABLED` (default off), capability `speakerFrameWindows`, protocol 41. Test kit run Q2
(2026-11-04, `kria-qendresa-test/_src/kria_check/Q2/REPORT.md` row B1): "Start with me full screen and the title ...
After that, put my screen recordings full screen and me cut out small in the bottom right corner". Today the recipe has
one `speaker_frame` for the whole edit and the first window opens at 0 s, so she is a small cut-out over Spec-kit from
the first frame, and the finished receipt answers the opening "met" ("Your clip is already vertical, so it fills the
frame") because the words read as the KRI-547 framing ask.

Her openings, measured (kit `_src/measure/ocr`, faces at 2 fps): V5 is full frame in her room with the big title for
0-7.2 s (face h 0.24, centre y 0.50), then the corner cut-out from 7.2 s (face h 0.17-0.18, centre x 0.78, y 0.64),
switching on the word she starts item 1 with ("first one" / "1. Spec-kit"). V1 is full frame for 0-2.5 s, then a split
screen to 7.65 s, then the corner (face h 0.14, centre x 0.80, y 0.72). V6 opens on the screen with no full-frame
opening. V1 and V5 also return to full frame for the closing call to action (last ~6 s). Her corner face is 1.5-2x
the size of today's `small_bottom_right` (width 0.42 of the canvas); the preset is NOT changed here.

**Contract.** `SpeakerBackgroundLayer.full_screen_windows: [{start, end}]` (output seconds; sorted, disjoint, each
>= 0.5 s, 1...4, end <= 1800; absent while unused, `null` = absent, `[]` invalid). Inside a window the speaker's own
take is drawn full-frame and untouched: no person mask, no background, no `speaker_frame`. Outside, the layer renders
exactly as before. Shared fixture `tests/fixtures/speaker-background-parity/v4.json`. The planner writes only the
opening (`background_media.full_screen_opening`, optional `full_screen_opening_s`); the list leaves room for her
full-frame ending without another protocol.

| # | Failure mode | Treatment | Pinned by |
|---|---|---|---|
| S70 | She is in the corner from 0 s although she asked to start full screen (Q2 B1) | Flag on, gate open, `full_screen_opening` on the strategy: the recipe gets `full_screen_windows: [{0, X}]`; the phone draws her raw take full-frame until X. | `tests/tasks/test_speaker_full_opening_worker.py` (real worker, recipe read back), kit `corner_speaker` + by-eye frames on a simulator render |
| S71 | The person mask fails at the switch (first corner frame unmasked, a flash of the room, or her silhouette vanishing) | No mask is computed inside the opening, so the matte's first frame is the switch frame; a failed mask there falls back exactly as today (`.original`: her plain take small in the corner over the background, never hidden while a corner frame is drawn). The matte's temporal state is keyed by clip and time, so the skipped opening is a continuity gap, never a stale silhouette. | `SpeakerBackgroundCompositionTests` (failing segmenter at the switch frame: the corner shows the plain speaker over the background), `testFullScreenWindowDrawsTheRawTakeWithoutMasking` |
| S72 | The switch lands mid-word | X is snapped to the gap between two kept words nearest the target (preferring a sentence end within 1.5 s); never inside a word's [start, end]. | `tests/services/test_speaker_full_opening.py::test_the_switch_never_lands_inside_a_word` |
| S73 | The opening is too short to read the title or so long the recordings start late | Target: the stated length (`full_screen_opening_s`, 1-15 s) when she named one; else the first recording's word if it is heard 2-8 s in (her V5 switches on "first one"); else 5 s; never shorter than the hook title (<= 3 s by default). Clamped to leave >= 1 s of take after it; a take too short for that (counted from the title's end when there is one: a 3.5 s take whose title ends at 3 s) gets no opening and a `take_too_short` receipt note, never a switch while the title is on screen. | `::test_the_opening_ends_on_the_first_recordings_word`, `::test_a_stated_length_wins`, `::test_a_short_take_gets_no_opening_and_says_so`, `::test_a_short_take_never_switches_before_the_title_ends`, `::test_the_opening_always_outlasts_the_title_and_leaves_a_corner` |
| S74 | The first recording plays its first seconds hidden behind the full-frame opening, or a window starts before X | With an opening, the lead-in rule starts the first window at X (not 0) and a video window starts from its own `source_start` there; a window laid out inside the opening is clipped to start at X, or dropped with `inside_full_screen_opening` when under 0.5 s remains. A name heard during the opening is seated at X (spoken order kept); in a rotation that entry joins the rotation, which shows every entry once over the part after X (S57). Flag off: the S50 lead-in exactly as before. | `::test_the_first_window_starts_at_the_switch`, item 17's frozen flag-off literals |
| S75 | The hook title covers her face during the opening | The Talking title is placed against the full-frame face samples (`place_talking_title`), which is exactly what shows in the opening; the opening lasts at least as long as the title, so it never moves onto the screen recording with a face check made for the wrong picture. | Simulator render: Apple Vision text boxes vs face box in 0-3 s; `::test_the_opening_covers_the_title` |
| S76 | The editor drops the new field on Save (iPhone editor rebuilds `speaker_background` without it; changing the fill or the corner clears it) | The editor document carries `full_screen_windows` (cut-timeline time); the iPhone `EditorSpeakerBackground` decodes, keeps and re-encodes it through every background change; Save validates it like `windows`; `remove_background` clears it; a build below 41 Saving a variant that carries it is 426 (`speaker_full_opening`), never a silent drop. | `tests/routes/test_speaker_full_opening_editor.py`, `SpeakerBackgroundEditorTests` (round trip + fill change keeps it) |
| S77 | An older app build (below 41, or `speakerFrameWindows` not verified) gets a recipe it cannot decode (426 on the whole video) | The planner offers the field only while `speaker_full_opening_planned_for(protocol)` holds; the worker re-checks against the approving build (`creator_client_protocol`) and lays the windows out without the opening (today's corner from 0); the device poll answers 426 only for a recipe already pinned with it. | `::test_an_old_build_gets_todays_corner_from_zero_and_an_honest_note`, `tests/services/test_phone_rollout.py` |
| S78 | The receipt says "met" for a corner from 0 s (the KRI-547 framing words) or for an opening that was dropped | A finished video drawing a corner frame never answers a framing ask "fills the frame" for the time she is in the corner; the opening ask is met only when `full_screen_opening_s` is on the variant, otherwise it names the corner and the reason (app too old, not available yet, take too short). The draft promises it only when the repair kept it. | kit `test_q2_full_screen_opening.py`, `tests/kria/test_brief_checks_full_opening.py` |
| S79 | Flag off changes anything | Off: no prompt bytes, no capability key, no field in any dump, no receipt key, the S50 lead-in and every recipe byte-identical. Not inert: prompt version labels (Main Creator v133, Brief extractor v60) and the FIELD_MATRIX rows move the capabilities digest once (as with KRI-1173). | `::test_flag_off_recipe_and_receipt_are_byte_identical`, prompt bytes with the flag off |
| S80 | A speech cleanup cut or an editor trim moves the switch into a word or past the end | The opening is laid out on the KEPT take and written back in original time like every window (`to_source_time`), remapped by `remap_lanes_for_cut`; an editor trim keeps it in cut time and clips it to the edit. | `::test_the_opening_survives_a_speech_cut` |
| S81 | The full-frame opening shows the background or the corner frame for a frame or two at its edges (instruction tiling) | The phone decides per frame from `full_screen_windows` (not per instruction range), so the first frame at or after X is the corner and every frame before is raw. | `testFullScreenWindowEdgesAreFrameExact` |
| S82 | The model sets the opening for a take with no background or no corner, or for "full screen" meaning the recordings | The field lives on `background_media` only and is taught for the speaker's own room before the background/corner; a strategy without a background drops it in the repair. "Put my recordings full screen" is the window media, not this field. | `tests/kria/test_speaker_background_chat.py` (repair drops it without a background) |
| S83 | Names said during the opening all sit at the switch; a name heard right after it leaves them a sliver, one gets under 0.5 s and is dropped (`window_too_short`), and since #2118 nothing comes back later, so that visual never shows (found merging #2118 under item 16) | The mention seated at the switch gets at least 0.5 s per visual: a mention starting sooner is pushed back to the switch + 0.5 s per seated visual (and so on down the chain), so every visual shows once, the pushed one at most that much after its word; nothing repeats. Never pushed into the last 0.5 s of the take: there the crowded visual keeps the honest `window_too_short` note. Flag off / no opening: unchanged. | `tests/services/test_speaker_full_opening.py::test_a_name_heard_right_after_the_switch_crowds_out_no_intro_name`, `::test_near_the_take_end_nothing_is_pushed_past_it`, `::test_an_opening_never_shows_a_photo_twice` |
| S84 | Moving the corner speaker by hand (#2117) on an edit with an opening rebuilds the whole preview on every drag release, or drops the opening from the quick in-place update | `SpeakerBackgroundLayer.replacingSpeakerFrame` keeps `fullScreenWindows`, so a moved frame on an edit with an opening is still "only a move" (`onlyMovesTheSpeakerFrame`), and `updateText` carries the instruction's windows over with the moved frame | `SpeakerFrameManipulationTests.testAnOpeningIsNotSelectableAndSurvivesAMovedFrame` |
| S85 | During the opening the creator taps the full-screen speaker and gets the corner speaker's box and handles (or the three drag stills paint a framed speaker that is not on screen) | `RecipeVideoInstruction.showsFramedSpeaker` is false inside a full-screen window, so there is no selection box and no drag split there; the compositor's slices paint nothing framed inside a window | `SpeakerFrameManipulationTests.testAnOpeningIsNotSelectableAndSurvivesAMovedFrame` |
| S86 | Green 5 (#2099) picks the automatic caption colour against the background with the take in its corner; inside the opening her take fills the screen instead, so over a dark room the captions come out plain white and sit on her bright wall for the first seconds (1.74:1; found merging #2099 under item 16) | `register_generation_background` records `full_screen_windows` (same timeline as `windows`) on the policy; `_background_frames` cuts those spans out of the background segments and adds her take frames for them instead, so one colour is chosen against what the phone draws (here white + black outline). No readable take with an opening: sampling is unavailable (`styling_background_sample_failed`, `no_take_for_full_screen`; the safe look), never the room. No opening: frames, colour and trace unchanged | `tests/tasks/test_speaker_full_opening_caption_contrast.py` (failed before the fix: 1.74:1 at 0.2 s), Green 5's `test_green45_caption_contrast.py` unchanged |
| S87 | S83's push-back stops at the take's last half second, but a later name heard before the one it pushed stays put: 8 s take, opening 6 s, three names in the opening, Miro at 6.6 s pushed to 7.5 s, Slack left at 7.2 s; Miro's window is reversed (7.5 -> 7.2) and Slack's overlaps, so the phone lane refuses the whole background, corner and opening included (Emir's review of #2111) | After the push-back, a mention that would start less than 0.5 s after the one before it joins that one: its names share the span, the ones without room get `window_too_short`, and every survivor is ordered, disjoint and at least 0.5 s. Unless the tail saturates every mention already starts that far apart, so nothing else changes (the one-later-name near-end case included) | `tests/services/test_speaker_full_opening.py::test_two_later_names_in_a_saturated_tail_keep_the_lane_valid` and the seeded `::test_a_crowded_tail_never_breaks_the_lane` (both failed before the fix: overlaps at 7.2 s and 7.7 s) |
