# Passage placement (KRI-1556)

One function decides which picture, if any, goes with a passage of the creator's speech and records
why: `place_passages` in `src/apps/api/app/services/passage_placement.py`. "No picture here" is a
decision. The speaker stays on screen.

## Modes

| Mode | Decides by | Built from |
| --- | --- | --- |
| `meaning` | What the words say. The card matcher (`phone_overlay_grounding._match_placements` and `OverlayPlacementAgent`, `heuristic_match` when there is no model) with the creator's request in its prompt. | Always |
| `slideshow` | The creator's order and pace. The only mode that rotates, spaces evenly or shows every picture. | An explicit order or pace ask (`PlacementRequest.slideshow`) |
| `phrase` | "When I say X show Y". Declared; path 4 (`phone_reaction_grounding`) moves in with unchanged behaviour. | A later step |

Candidates reach the matcher sorted by asset id, so re-ordering the pool never changes the prompt
or the result.

## Flag

`PASSAGE_PLACEMENT_ENABLED` (default `false`). On: first-draft cards (`ground_phone_subtitled_overlays`,
called from `_run_phone_subtitled_job`) pass the creator's request to the matcher, sort the
candidates, and write `phone_overlay_receipt["passage_placement"]` with a decision, a rule and a
reason per passage, plus the pictures left unused. Off: the matcher call, the prompt and the receipt
are byte-identical to before (`test_flag_off_is_byte_identical_to_the_unflagged_call`).

Apply: `fly secrets set PASSAGE_PLACEMENT_ENABLED=true --app nova-video` (api and worker) and restart.
Rollback: `...=false` and restart.

## Scope

Moved so far: paths 1 and 2 (the card matcher and its keyword fallback, first-draft cards). Not
moved: the full-screen sequence, "when I say X show Y", the background sequence, the chat
`set_background_sequence` op, multi-clip cutaways and the speech-cut remap. They keep their own rules
until their step lands.

`needs` (passages that want a picture) stays empty until the matcher's wishlist is typed with times.

## Test matrix

`tests/services/test_passage_placement_matrix.py`. One row per failure mode: `shuffle`, `decoy`,
`none`, `paraphrase`, `slideshow`, `pace`, `after_cut`. Model rows replay hand-authored recorded
answers (`tests/fixtures/passage_placement/replays.json`) through the real agent; what each passage
is about is in `expected.json`, independent of any matcher output. Strict xfails name the gaps that
remain: the keyword fallback places a decoy that shares two words, opens a card on the previous
sentence, and `remap_lanes_for_cut` shifts a window instead of dropping it when its anchor words were
cut.
