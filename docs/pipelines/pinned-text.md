# Pinned corner text (KRI-523, KRI-525, KRI-526, KRI-527)

Creator text that sits in a named corner (`top_left` ... `bottom_right`) while the video plays,
as opposed to the opening title (centred, a short hold) and per-shot labels (fixed position).

## Contract

- **Field:** `CreativeStrategy.pinned_texts: list[PinnedText]`; each `{text, corner}` plus an
  optional scope: `start_s` / `end_s` (a missing side is the start / end of the video) **or**
  `clip` (1-based, in edit order). No scope = the whole video. Seconds and `clip` never mix.
  The range fields are omitted from dumps when unset, so a whole-video pin serialises exactly as
  before. The field is `SkipJsonSchema`: the `apply_strategy` tool schema and the generated
  contracts do not change (`python -m app.cli.kria_contracts --check`).
- **Limits:** the strategy parses leniently (12 lines, 400 characters) so an over-limit ask
  reaches `creator_capabilities` and becomes a question (`pinned_text_too_many`,
  `pinned_text_too_long`); the snapshot, brief and every renderer are strict at 4 lines of at
  most 120 characters (`require_drawable_pins`).
- **Grounding:** the text must be the creator's own words (`planner.ground_pinned_texts`), and
  any seconds must be numbers they wrote (`routes/creator_agent._pin_range_is_grounded`). A clip
  scope is deliberately not matched against words (no language-independent form); an index
  past the selected clips is a question (`pinned_text_clip_missing`), and one past the real cuts
  is dropped in `plan_unified_montage`, so the brief receipt reports it missing.

## Rendering

`pipeline/pinned_text.py` is the single place: `pins_from_strategy` -> `resolve_pin_windows`
(a window per pin, from the real clip timings) -> `pinned_text_elements` (`guided-pinned-<i>`).
Stacking is greedy per window: pins in one corner share a line unless they overlap in time. Labels,
montage text, beat thoughts and narration captions leave the bottom zone (y 0.70) only while a
bottom pin is on screen; the opening title steps below a top pin that shares its hold
(`title_y_clear_of_pins`, also applied by face placement).

| Writer | How pins are added |
| --- | --- |
| Cloud / unified / lip-sync guided montage | `guided_story._text_elements` (clip windows from the compiled story timeline) |
| Spoken-excerpt montage, voice-behind-footage | `phone_speech_montage_job._with_creator_pins` -> `phone_narrated_plan.with_pinned_text_layers` (`title-N` layers) |
| Recorded-voiceover montage | `generative_build._run_phone_voiceover_montage_job` -> `with_pinned_text_layers` |

Every writer also persists the pin rows as the variant's `text_elements` (the editor preview
compiles text from the row, not the recipe). **Known limit:** the voiceover montage's intro text
is style-set / agent positioned and is not moved away from a pin.

## Editor

`kria_editor_timeline.rebase_guided_text` and `GuidedLabelRebase.swift` keep a pin that spans the
whole video anchored to the timeline; a pin that fills one clip follows that clip; any other
ranged pin is re-windowed through source time. Parity vectors:
`tests/services/test_guided_label_rebase_vectors.py` regenerates
`src/apps/ios/Tests/Fixtures/guided_label_rebase_vectors.json`.

## Semantic agent

`EditProposalAgentInput.pinned_texts` is server-owned copy for `semantic_edit_proposal`: a
quoted pin is not an allowed beat caption, and a thought or text binding that repeats one is
blanked / dropped (`blanked_pinned_text_thought`).

Tests: `tests/kria/test_pinned_texts.py`, `test_pinned_text_ranges.py`, `test_pinned_text_limits.py`,
`tests/agents/test_semantic_edit_proposal_pins.py`, `tests/services/test_phone_montage_writers_pins.py`.
Pre-PR: `make verify-overlays` (fixtures `kri523_pinned_corner_text.json`, `kri525_ranged_pins.json`).
