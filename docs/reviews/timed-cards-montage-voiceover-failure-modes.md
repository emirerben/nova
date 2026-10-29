# Exact-timed small cards on phone Montage and Voiceover edits — failure modes

Written BEFORE the code (project testing rule). "Small card at an exact time" = a creator-owned
photo or Visuals video shown picture-in-picture for a stated hold at a stated moment ("show the
menu photo as a small card at 3 seconds for 2 seconds", "put the receipt in the corner during the
second clip") on a phone-rendered **Montage** (`_run_phone_guided_job`, guided / unified montage;
`_run_phone_voiceover_montage_job`, montage-family voiceover) or **Voiceover** (narrated,
`replace_editor_lanes`) edit. Flag: `PHONE_TIMED_CARDS_ENABLED` (default `false`).

The request model is `TimedCard` (`app/agents/_schemas/reaction_beats.py`): `visual_id` plus
ONE anchor — `at_s` on the output timeline, or `clip_id` + `offset_s` (seconds after that clip
starts in the output) — and `hold_s`. Word-triggered `ReactionBeat`s are untouched. The worker
grounds cards with `app.pipeline.phone_timed_cards.ground_timed_cards` into
`SubtitledOverlayCard`s and compiles them through the existing `_compile_overlay_track`.

Each mode below names the test that pins it (all in
`tests/pipeline/test_phone_timed_cards.py` unless stated).

| # | Failure mode | Pinned by |
|---|---|---|
| 1 | **Wrong clock after a cut / trim / transition.** A `clip_id` anchor must resolve against where that clip actually STARTS on the compiled output timeline (crossfade overlaps shift it earlier; a source refit can shrink a moment), not the plan's nominal slot. | `test_clip_anchor_follows_the_compiled_clip_start_through_a_crossfade` |
| 2 | **Card covers captions.** `_compile_overlay_track` already clamps `y_frac <= CAPTION_BAND_TOP_FRAC`; the grounding must never emit a card below it, and the compiled placement must stay above the band. | `test_card_stays_above_the_caption_band` |
| 3 | **Card covers a face.** With face boxes for the window, the card moves to the other corner, then the middle band; if every candidate still overlaps a face, the card is DROPPED (`face_collision`) and the receipt says "Couldn't", never drawn over the face. | `test_card_moves_off_a_face_then_drops_when_every_spot_collides` |
| 4 | **`at_s` beyond the edit's length.** A card that starts at or after the timeline end (or inside the last 0.3 s) is not placed (`past_end`) and the receipt names the video's real length; a card that starts inside but would outlast the end is shortened to the end. | `test_card_past_the_end_is_unplaced_and_a_late_card_is_shortened` |
| 5 | **Video card without `visualVideos`.** A Visuals video card where the device has not verified `visualVideos` is dropped at repair (notice) and never bound; the grounding refuses a video binding when video cards are not allowed (`video_not_supported`). | `test_video_card_needs_visual_videos_verified` + `tests/services/test_creator_capabilities.py::test_timed_video_card_repair_requires_verified_visual_videos` |
| 6 | **Card on a photo-only moment.** A clip anchor that names a Visuals photo moment (not footage) is unplaced (`clip_not_in_edit`); an `at_s` card over a photo moment is fine (the engine composites cards over stills). | `test_clip_anchor_must_name_an_edit_clip` |
| 7 | **Two cards overlapping in time.** Cards are a sequence, not a stack: the later-starting card that overlaps an earlier one is unplaced (`overlaps_card`), never drawn on top. | `test_overlapping_cards_keep_the_first_and_report_the_second` |
| 8 | **Turkish asks.** The repair notices and the receipt lines have Turkish twins (`strategy_policy._KNOWN_TR`, `say(en=, tr=)`), so a Turkish chat never gets an English "Couldn't". | `tests/kria/test_brief_timed_cards.py::test_turkish_chat_gets_turkish_receipt_lines` |
| 9 | **Gate off ⇒ refusal byte-identical.** With `PHONE_TIMED_CARDS_ENABLED=false` the manifest carries no `timed_cards` key (hashes unchanged), `media_overlays` stays `unsupported_on_phone` for guided/narrated, a strategy with `timed_cards` is stripped with a notice, and the compiled recipe for a plan without cards is byte-identical to before (`recipe_digest`). | `tests/services/test_creator_capabilities.py::test_timed_cards_capability_absent_and_strategy_stripped_when_flag_off`, `test_compile_without_cards_is_byte_identical` |
| 10 | **Narrated voiceover vs montage voiceover routing.** A narrated (`narrated*` + voiceover) edit compiles cards through `replace_editor_lanes` on the `narrated` track; a montage-family voiceover through `compile_phone_voiceover_montage_plan(timed_cards=)` on the `montage` track; a plain montage through `compile_phone_guided_plan(timed_cards=)` on the `story` track. Each lands the card on ITS main track's clock. | `test_each_runner_lands_the_card_on_its_own_main_track` |
| 11 | **Editor round-trip.** The grounded cards persist on the variant (`phone_timed_cards`), so a guided text-only Save recompiles them (`phone_editor._compile_editor_commit`) instead of silently dropping the lane; voiceover edits persist them as the existing KRI-281 `media_overlays` lane rows. | `tests/services/test_phone_editor_timed_cards.py::test_guided_text_only_save_keeps_the_timed_card` |
| 12 | **Honest receipt.** Draft: "Small card of <photo> at 3.0 s for 2.0 s" is said only when the card survives repair; a card the gate refuses reads "Couldn't"; a rendered variant's receipt reads the worker's `phone_timed_card_receipt` (placed/unplaced with reasons), never the strategy. | `tests/kria/test_brief_timed_cards.py` |
| 13 | **Visual id the manifest does not own.** A `visual_id` that resolves to nothing is dropped at repair with a notice naming the time; a label match canonicalises to the manifest's `media_id`. | `tests/services/test_creator_capabilities.py::test_timed_card_repair_resolves_labels_and_drops_unknown_visuals` |
| 14 | **Manifest advertises what the worker would refuse.** `timed_cards` is available only where `phone_timed_cards_supported()` (flag + `media_overlays_enabled` + every `PHONE_SUBTITLED_OVERLAY_FEATURES` verified) holds AND the format is a phone guided format or a phone Voiceover with a recorded voiceover; Talking never advertises it (beats own that edit). | `tests/services/test_creator_capabilities.py::test_timed_cards_capability_matrix`, `tests/services/test_phone_rollout.py::test_phone_timed_cards_supported_gate` |

Device assertion (manual, after a render with the flag on): extract the frame at `at_s + 0.2 s`
from the exported MP4 and the frame at the same time from a render without the card; the card's
region (`x_frac ± scale/2`, `y_frac ± …`) must differ while the rest of the frame is identical.
