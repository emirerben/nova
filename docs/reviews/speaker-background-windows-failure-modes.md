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
  unanchored and gets a `phone_lane_receipt` note (`phrase_not_heard`). Pool ids used by windows join
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
| S38 | Chat: "whichever photo I'm talking about" anchored to a phrase that is never said | The entry is laid out as unanchored (cadence) with a `phrase_not_heard` receipt note; never a failed job, never a silent drop. | `tests/tasks/test_speaker_background_strategy_worker.py::test_unheard_phrase_falls_back_to_cadence_with_a_note` |
| S39 | Chat: a sequence longer than the take, or a take longer than the sequence | Windows past the take end are cut at the take end (dropped when nothing remains); a shorter sequence cycles with `cadence_s` until the take ends. | `tests/tasks/test_speaker_background_strategy_worker.py::test_sequence_cycles_to_the_take_end`, `::test_windows_past_the_take_end_are_clipped` |
| S40 | Editor parity drift: new iOS session functions without agent ops | Parity group `speaker_background` gains `setSpeakerBackgroundWindows`, `setSpeakerFrame`, `clearSpeakerFrame`. | `test_ios_editor_actions_have_agent_ops` |
| S41 | Capability drift | `speakerBackgroundWindows` in both `MediaCapability` mirrors and the capability-matrix doc. | `tests/kria/test_capability_matrix.py` |
| S42 | Protocol collision | Server's newest is 28 (`media_tilt_min_client_protocol`), app `version = 28`; the open card-tilt PR #2033 (KRI-1141) takes 30, #2038 (KRI-1139) takes 31 and 29 is unused, so this feature takes **32**. Re-check open branches before merge. | `KriaTests` header assertions |
| S43 | The fill asset is also a window asset, or a window asset is also a card | Allowed for fill == window (same photo may fill and open); window/fill ids are reserved from `used_media_ids` so they are never also a cutaway or card. | `tests/tasks/test_speaker_background_strategy_worker.py::test_window_ids_are_reserved_from_cards` |
| S44 | Live camera preview shows one background while the plan will switch | The camera pick stays single (the fill). The teleprompter shows "Preview" and never claims parity (S24); nothing new to draw there in this cut. | n/a, copy review |

Device tests owed before `speakerBackgroundWindows` is verified: on an iPhone, a Talk-to-camera take with
three photo windows and the speaker small bottom-right must show each photo in its window and the creator
cleanly cut out in the corner in preview and export; a video window must loop inside its window and be
silent; a 60 s export with ten windows must finish within 120 s without a thermal step-down.
