# KRI-840 failure-mode list (written before any fix code)

Ticket: on iPhone Talking, "show photo X FULL SCREEN when I say W" must (1) fill the screen
within 0.5 s of W for the requested hold ("a moment" <= 1.5 s) with voice + captions unbroken,
(2) never report a corner card as full screen.

Test file: `src/apps/api/tests/kria/test_kri840_fullscreen_beat.py` (T = covered there;
NEW = the fix author must add a test; DEC = design decision, see the end).
Line refs are on main 004f38182. "Field" = `ReactionBeat.display_mode: "pip" | "fullscreen"`
(default "pip", omitted from every dump) - the name PR #1756 (now merged, 16df40baa) added for Voiceover.

## A. Authoring (Main Creator prompt + wire schema)

| # | Failure mode | Where | How a test catches it |
|---|---|---|---|
| A1 | Model is never taught the field, so "tam ekran" / "full screen" still yields a corner beat | `main_creator.py:218` `_REACTION_BEATS_PROMPT_SECTION` (Talking) and `:258` Voiceover slice | NEW: replay-eval fixture `tests/fixtures/agent_evals/main_creator/kri840_*.json` built like `kri178_talking_reaction_beats.json`; free structural replay in `tests/evals/test_main_creator_evals.py` |
| A2 | Over-application: model marks the "small card in the corner" beat fullscreen, or every photo fullscreen | same prompt section | NEW: same fixture carries the Don Julio chimichurri corner beat and asserts `display_mode` absent/pip on it |
| A3 | Ask side not recorded: brief fact `display_mode` missing, so the receipt layer has nothing to compare (KRI-667 forbids new word regexes in `brief_checks`) | `main_creator.py:360` `_BRIEF_PROMPT_SECTION` (facts list) | T `test_guard_an_ask_that_names_no_display_gets_no_display_verdict` (no fact = no verdict); NEW: eval fixture asserts `brief_updates[].facts.display_mode` |
| A4 | Model writes a value outside the Literal ("corner", "full") - strict adapter rejects the whole strategy | `reaction_beats.py` Literal | NEW: parse test that "corner" is rejected cleanly (retry path), prompt names exact values |
| A5 | Prompt text changed without bumping `MAIN_CREATOR_PROMPT_VERSION` (now `2026-10-22-v69`, `main_creator.py:106`; pinned in `tests/agents/test_main_creator_agent.py:122`) | version constant | existing pin test; also merge-order collision with #1755/#1756 (both bumped; now v75, this fix is v76) |
| A6 | Wire schema (typed-presence-v2) mis-projects the new enum | `_creator_wire.py:44` `MODEL_HIDDEN`, fragment built from `TypeAdapter(field.annotation)` | existing `tests/agents/test_creator_wire_contract.py` (round-trips every main_creator fixture, provider schema audit); the new fixture rides it for free |
| A7 | Sticker-role or sound-only beat marked fullscreen (a transparent PNG cover-filled; nothing to show) | validator / repair | DEC-3; NEW test once decided |

## B. Parse, repair, policy, hashes

| # | Failure mode | Where | Catch |
|---|---|---|---|
| B1 | Field rejected: `ReactionBeat` is `extra="forbid"` | `reaction_beats.py:37-43` | T `test_a_reaction_beat_can_ask_for_the_full_screen`, `..._strategy_round_trip` |
| B2 | Default is serialized: stored strategies, `canonical_context_hash`, approved-snapshot/contract digests and comparison rows move for every legacy strategy | `creator_agent.py` dumps with `exclude_none=True` at `creator_sessions.py:913`, `kria/drafts.py:101`, `routes/creator_agent.py:5062/5136/5204`, `services/generative_jobs.py:552`; `"pip"` is NOT None, so it needs the omit-at-default `model_serializer` (as #1756 does, merged); plain dumps also used at `approval_readiness.py:159`, `creator_comparison_persistence.py:171` | T `test_guard_a_legacy_strategy_dumps_and_hashes_byte_identically` (pinned JSON + sha256 `ae5e66...69cc5` for BOTH dump modes) |
| B3 | New leaf not registered: `FIELD_MATRIX` guard fails the build | `creator_render_contract.py:456-470` (`reaction_beats[].display_mode` as `upstream_resolved`/`reaction_beats_grounding`) | existing `tests/services/test_creator_render_contract.py::test_every_strategy_path_is_assigned` |
| B4 | Repair drops the field or the whole beat. `_repair_creator_reaction_beats` (`creator_capabilities.py:931`) uses `model_copy(update=...)` so the field survives; BUT #1756's version (merged) keeps a fullscreen beat only for native Voiceover and DROPS it on Talking ("not available; left it out"); this fix downgrades it to a corner card there instead | `creator_capabilities.py:~985-1030` | T `test_the_draft_says_done_for_a_fullscreen_beat...` (Talking manifest with `media_overlays:fullscreen`) |
| B5 | Capability off (`PHONE_SUBTITLED_FULLSCREEN_OVERLAYS_ENABLED=false`, no `media_overlays:fullscreen`): beat dropped => draft receipt says "I couldn't find the photo" (misleading) instead of "small card instead of full screen" | `creator_capabilities.py:574` capability, `:1283` precedent (refuses for `overlay_display`) | DEC-2; NEW test: capability off => receipt partial, reason names corner/full screen, never "couldn't find" |
| B6 | Rollback hazard: after deploy, a stored/approved strategy carries `display_mode`; rolling the API/worker back to old code makes `CreativeStrategy.model_validate` fail (extra forbid) at the worker | deploy | rollout note: kill switch is the flag, never a code rollback (same as other KRI flags); no test |
| B7 | Old clients: iOS never decodes strategies or beat receipts (`Generated/openapi.yaml` has no reaction-beat schema; no `phone_beat_receipt` in src/apps/ios or web), and the recipe uses the existing `subtitled-fullscreen` track (KRI-297) with the same `visualBlocks/alphaOverlay/audioMix` capabilities | n/a | no test needed; verified by grep + `DeviceMontageRenderE2ETests.testSubtitledFullscreenVisualsRenderOnTheIPhone` |
| B8 | `clip_scope.placed_media` / `strategy_policy._placed_media_ids` / `voiceover_scope` read `beat.visual_id` only - unaffected, but a fullscreen beat's photo must still count as "placed" | `kria/clip_scope.py:374`, `strategy_policy.py:348`, `voiceover_scope.py:57` | existing tests; no change expected |

## C. Grounding (word -> card)

| # | Failure mode | Where | Catch |
|---|---|---|---|
| C1 | Display lost at the grounding hop: card is a face-aware corner card (`_PHOTO_SLOT` 0.74/0.22, scale 0.36) | `phone_reaction_grounding.py:594-600` (reads only visual_id/role/hold), `:923` `SubtitledOverlayCard(...)` has no `display_mode` | T `test_the_fullscreen_beat_becomes_a_fullscreen_card[cafe,julio]` |
| C2 | Gate not passed from the Talking worker: if grounding takes an `allow_fullscreen` gate (#1756, default False) and `generative_build.py:7825` doesn't pass it, every fullscreen beat is `fullscreen_not_enabled` | `generative_build.py:7825` (Talking) vs `:9049` (Voiceover) | T worker tests `test_worker_julio_t2_*`, `test_worker_cafe_t2_*` (real worker) |
| C3 | Hold ignored / wrong default: `hold_s` is honored for corner (so "for a moment" 1.2 s works) but a beat with no `hold_s` gets `_PHOTO_HOLD_S_DEFAULT = 3.0` (`:83`, `:646`); "a moment" held 3 s | `:640-646` | T `test_julio_for_a_moment_is_held_no_longer_than_a_moment`, `test_a_fullscreen_beat_without_hold_s_is_a_moment_not_the_photo_default[*]` |
| C4 | Trigger start > 0.5 s off the word | `_match_trigger` start of FIRST trigger token; card `start_s = match_start_s` (`:660`) | T guard `test_guard_the_card_starts_on_the_spoken_word[*]`; NEW once cut/remap involved: T `test_julio_t2_opening_cut_*` (5.0 s -> 0.8 s after a 4.2 s opening cut). Note whisper.cpp word starts equal the previous word's end, so real onsets can be late; prod whisper-1 is tighter |
| C5 | Fullscreen truncated by a later corner card (shared "photo" slot) | `_truncate_same_slot_overlaps` `:476`; slot picked at `:600` | T `test_a_corner_card_never_shortens_the_fullscreen_photo` (cut to 1.38 s today) |
| C6 | Corner card shown ON TOP of a fullscreen photo (PiP track draws above `subtitled-fullscreen`) | track order `phone_subtitled_plan.py:565-606` | DEC-1; NEW test once decided |
| C7 | Fullscreen goes through face/caption-safe arbitration and can be reported `no_safe_spot` | `:886` `resolve_phone_card_geometry` | T `test_a_fullscreen_card_needs_no_safe_spot_for_a_corner` |
| C8 | Fullscreen video beat: not cover-filled, longer than footage, or not muted | `_compile_fullscreen_track` handles video; grounding `:641-644` caps hold to footage | T `test_a_fullscreen_video_beat_plays_full_frame_from_its_start` |
| C9 | `occurrence: "every"` + fullscreen: windows overlap => `SubtitledLaneError("full-screen overlay windows must not overlap")` drops the WHOLE overlays lane (corner cards too) | `phone_subtitled_plan.py:866-871`, retry/drop at `generative_build.py:8199` | T `test_every_occurrence_of_a_fullscreen_beat_never_overlaps_itself` |
| C10 | Beat fullscreen overlaps a `fullscreen_images` sequence card (sequence ignores `occupied` for full-frame cards, `phone_overlay_grounding.py:409-480`): same whole-lane drop | `generative_build.py:7877` `beat_windows` passed as `occupied`; sequence ignores it for fullscreen | T `test_a_fullscreen_beat_and_the_fullscreen_images_sequence_never_collide` (sequence card 5.64-8.64 sits on the beat today) |
| C11 | Beat-claimed visual wrongly stays in the `fullscreen_images` sequence (shown twice) | `used_media_ids = ... | beat_media_ids` (`:7922`) already excludes it | covered by the same T; existing worker test `test_subtitled_reaction_beats_happy_path` pins `used_media_ids` |
| C12 | `MAX_REACTION_PLACEMENTS = 48` / `MAX_REACTION_OCCURRENCES = 24` and the 100-clip track cap: fullscreen cards share the placement budget with corner cards; fullscreen track is separate so it cannot starve PiP | `phone_reaction_grounding.py:79-80`, `:627` | NEW: only if "every" + fullscreen is allowed (DEC-4); otherwise force `occurrence: "first"` for fullscreen |
| C13 | Closing-photo merge: same photo is a fullscreen beat AND the closing shot; merge looks only at slot "photo" so a second `closing-photo` card duplicates it | `:779-790` | NEW test (rare; DEC-5) |
| C14 | Cleanup/opening cut drops or shifts the fullscreen window; receipt keeps saying placed | `remap_lanes_for_cut` (`phone_subtitled_lanes.py:234`, keeps `display_mode` via `model_copy`); `_demote_beat_receipt` | T `test_julio_t2_opening_cut_*`; demotion keeps `display_mode` on survivors: `_demote_beat_receipt` (`generative_build.py:6684`) REBUILDS placed entries from a fixed key list and would drop `display_mode` - NEW test |
| C15 | Multi-clip Talking head: footage cutaways must leave the fullscreen beat's slot | `generative_build.py:8136` passes fullscreen card windows to `_schedule_talking_footage` | existing `phone_talking_cutaways` tests; NEW: one case with a fullscreen beat |
| C16 | Voiceover lane: `replace_editor_lanes` (`phone_narrated_plan.py:999`) compiles ALL overlays with `_compile_overlay_track` and `_EDITOR_LANE_TRACK_IDS` (`:901`) lacks `subtitled-fullscreen`: a fullscreen card on Voiceover would be drawn as a small card | `phone_narrated_plan.py` | out of scope for Talking; #1756 (merged) fixes this for native Voiceover |

## D. Compile / recipe (what the phone draws)

| # | Failure mode | Where | Catch |
|---|---|---|---|
| D1 | No fullscreen track in the recipe (card not `display_mode="fullscreen"`) | `phone_subtitled_plan.py:559-585` | T `test_cafe_t2_oblea_fills_the_screen_*`, `test_worker_*` |
| D2 | Fullscreen drawn above PiP/captions, or not cover-filled | `visual_placement` without `width_fraction`; text layers always composite last | T assert `width_fraction is None`, track order, caption layers cover the window; iOS: `OverlayCaptionLayeringTests.testCaptionStaysOverAFullscreenCardUnlessTheCardIsAboveText` |
| D3 | Speaker video/audio interrupted: speaker stays the one main-track clip, fullscreen clip `volume=0`, `AudioMixRecipe(original_volume=1.0)` | `:536`, `:633` | T `test_voice_and_captions_run_unbroken_under_the_fullscreen_photo[*]` |
| D4 | Captions stop or lag under the fullscreen photo | captions are `text_layers` built from cues, independent of lanes (`:459-481`) | same T (coverage of the window by caption layers) |
| D5 | Pilot validation rejects the recipe (capability not verified) | `validate_phone_pilot_recipe` | T Café compile test runs it; worker tests run it for real |
| D6 | Fullscreen photo with the cleanup cut inside its window shrinks below 0.3 s and vanishes silently (`_MIN_FULLSCREEN_S`) while the receipt says placed | `phone_subtitled_plan.py:838,864`; `remap_lanes_for_cut` drops only fully-removed windows | NEW test |
| D7 | Editor Save: a persisted fullscreen beat card must round-trip (NEW full-screen cards are refused, an existing one is kept) | `phone_subtitled_editor.py:332-355` (recipe -> lanes), `:474-487` | existing KRI-297 editor tests; NEW: one beat-born card round-trip |

## E. Receipts

| # | Failure mode | Where | Catch |
|---|---|---|---|
| E1 | Corner card reported "Done" for a full-screen ask (the headline bug) | `brief_checks.py:3477 _check_reaction_beats` never reads display; `BeatFact` (`:104`) has no display; `_phone_receipt_facts` (`:1382`) drops it | T `test_a_corner_card_is_never_reported_as_full_screen` (+Turkish), `test_worker_never_lets_a_corner_card_pass...`, draft `test_the_draft_never_says_done_for_a_corner_beat...` |
| E2 | Fullscreen reported "Done" for a corner ask | same | T `test_a_fullscreen_card_is_never_reported_as_the_corner_card_that_was_asked`, draft twin |
| E3 | Over-reach: an ask that names no display gets a verdict, or a correct display is demoted | new check | T guards `test_guard_the_right_display_is_met`, `test_guard_an_ask_that_names_no_display_gets_no_display_verdict` |
| E4 | `fullscreen_images` (even slots) satisfying a word-timed full-screen ask, or reading "can't check" / "no pop-ins timed to your words" | `brief_checks.py:2988` `_CANT_CHECK_BEATS`; draft path `_beat_facts` yields `()`; rendered path has no beat receipt | T `test_even_slot_fullscreen_images_are_not_reported_as_the_word_timed_ask`, `test_a_draft_with_only_fullscreen_images_...` (needs a new PlanFacts field from `phone_overlay_receipt`/`overlay_display`) |
| E5 | New fact not recorded by the model: receipt stays neutral (honest, not a false Done) | KRI-667 doctrine | T guard (E3) |
| E6 | "For a moment" held too long reported "Done" | needs a recorded fact (`hold_s`), no regex allowed (`tests/kria/test_kri667_no_new_intent_regexes.py` ratchet) | DEC-6 (recommend: enforce in grounding, do not add a receipt check) |
| E7 | Receipt rebuilt without `display_mode` after a demotion | `_demote_beat_receipt` survivor dict (see C14) | NEW test |
| E8 | Creator-facing render notes (`nova_steps._humanize_subtitled_reaction_beats`, `beat_miss_sentence`) get a new unplaced reason (`fullscreen_not_enabled`, `overlap`) and fall to the generic "Couldn't add X to the phone render" | `services/nova_steps.py:395-420` | NEW: add the reason to the mapping + test |
| E9 | TR/EN wording: both through `say(en=..., tr=...)` | `brief_checks` | T Turkish case (`köşe` named) |

## F. Process / rollout

| # | Failure mode | Catch |
|---|---|---|
| F1 | Two PRs add the same field under different names/shapes (#1756 `display_mode` vs a new `display`) and conflict in 6 files | build on `display_mode` / "pip"|"fullscreen" or merge #1756 first (done: it is merged, the fix layers on it) |
| F2 | Version collisions: `MAIN_CREATOR_PROMPT_VERSION` (#1755 and #1756 also bump), `PlanFacts` dataclass (#1757/#1758/#1759/#1760 add fields at `brief_checks.py:~329`) | rebase last; new field appended, not inserted |
| F3 | Fly first, then restart api + worker; flag `PHONE_SUBTITLED_FULLSCREEN_OVERLAYS_ENABLED` (default true) is the kill switch; no web/iOS flag | runbook line in the PR |

## Open design decisions (my recommendation first)

- DEC-1 Corner card vs fullscreen overlap. Recommend: a corner card never shows during a fullscreen window - an earlier corner card is cut at the fullscreen start, one that would start inside it is dropped as `overlap` (reported). Today PiP draws above fullscreen, which looks cluttered.
- DEC-2 Capability off / not Talking. Recommend: keep the photo as a corner card (`display_mode` -> pip) with a notice and let the receipt say "partial: small card, not full screen"; do not drop the beat (dropping reads as "couldn't find the photo"). Note #1756 (merged) dropped on Talking; the repair now downgrades there too.
- DEC-3 Sticker role or sound-only with fullscreen. Recommend: repair to pip (never reject), same as other repairs.
- DEC-4 `occurrence: "every"` with fullscreen. Recommend: allow, windows already de-overlapped by the same-slot truncation; cap stays 24.
- DEC-5 Closing-photo merge with a fullscreen beat. Recommend: do not merge (a separate closing card is correct); rare.
- DEC-6 Hold-too-long receipt. Recommend: no new receipt check; default fullscreen hold 1.5 s, `hold_s` honored (prompt teaches "a moment" = 1.2-1.5), receipt entries already carry `at_s`/`end_s`.

## Decisions taken (after this list, before the fix)

- DEC-1: no cross-slot arbitration. A corner card whose window overlaps a full-screen photo
  still draws on top of it, the same as the existing KRI-297 track order.
- DEC-2: downgrade to a corner card with a notice; never drop the beat.
- DEC-3: a sound-only beat is repaired to "pip". A sticker-role beat keeps "fullscreen".
- DEC-4: `occurrence: "every"` is allowed. The full-screen slot's own truncation stops
  windows overlapping.
- DEC-5: a closing photo never merges into a full-screen beat card. The closing window
  removing one reports `fullscreen_overlap`.
- DEC-6: no hold-too-long receipt check. The full-screen default hold is 1.5 s, and the
  prompt maps "a moment" to `hold_s` 1.0–1.5.
