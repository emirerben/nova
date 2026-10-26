# KRI-941: a listed order ("in this order: A, B, C") must land, and the receipt must be honest

Prod 2026-10-25, job b3820ddc (thread 33BA8AFB, Don Julio walk-in M1): "Start on the DON JULIO
window sign ... end on my friend ... In between, in this order: the door, the butcher, the grill,
the flowers on the ceiling, the Michelin plaques." The planner emitted three `order` rows: first
(sign), last (friend) and ONE position-less row naming all five middle clips. `apply_sequence`
seats a row's members in the order they arrived (attachment order) and only orders ACROSS rows,
so the middle played plaques, door, grill, butcher, flowers. `_sequence_outcomes` then marked that
row `met / landed` because a position-less row was always met (and with every clip named, the
first / last rows were vacuously met too). The receipt told the creator the order landed.

Fix (two parts, no change to the seating rule):

* A. The planner prompt says one `order` intent PER NAMED SUBJECT for a listed order (it did at
  lines 80-84 and contradicted itself at 204-206). Prompt-only: model behaviour.
* B. `unified_montage._sequence_outcomes` judges every row against the finished cut: a first row
  leads ALL other clips, a last row trails ALL other clips, a then row sits after the first rows'
  clips and earlier then rows' clips and before the last rows' clips.

## Failure-mode list (written before the code)

Each row names the test that catches it. `U` = `tests/pipeline/test_unified_montage_sequence.py`,
`C` = `tests/services/test_order_sequence_contract.py`, `R` = `tests/pipeline/test_kri941_listed_order_replay.py`.

| # | Way the fix could fail or regress | Caught by |
|---|---|---|
| 1 | A then row's clips land BEFORE the first row's clip (or AFTER the last row's) and the row still reads `landed` | `U::test_a_then_clip_seated_before_the_first_clip_is_misplaced`, `U::test_a_then_clip_seated_after_the_last_clip_is_misplaced`, `C::test_a_listed_row_seated_before_the_first_clip_is_refused_by_the_receipt` |
| 2 | Two listed then rows (five one-clip rows) seated reversed / shuffled and every row still reads `landed` | `U::test_listed_then_rows_seated_reversed_are_misplaced`, `C::test_five_listed_rows_land_in_the_listed_order_from_any_attachment_order` (positive), `C::test_five_listed_rows_out_of_place_are_misplaced_when_the_seating_rule_is_off` (negative control) |
| 3 | A first / last row is vacuously `landed` when every clip in the edit is named by some row (`others` empty) | `U::test_a_first_clip_seated_second_is_misplaced_when_every_clip_is_named`, `U::test_a_last_clip_seated_second_to_last_is_misplaced_when_every_clip_is_named` |
| 4 | A multi-member first (or last) group that is a contiguous block is judged wrong (every member must lead, as a block) | `U::test_a_multi_clip_first_group_is_met_as_a_block_and_misplaced_when_split` |
| 5a | first(A) + last(B) + then("this chapter order", A, B) (prod job aee52a3c) stops reading met | `C::test_a_then_group_made_only_of_clips_an_earlier_group_seated_does_not_fail_the_order` (existing) |
| 5b | A then row repeating the start clip stops reading met | `C::test_a_then_group_that_repeats_the_start_clip_does_not_fail_the_order` (existing) |
| 5c | A visuals-only then row stops reading met | `C::test_a_then_group_of_visuals_only_does_not_fail_the_order` (existing) |
| 5d | A then row that introduces new clips stops reading met | `C::test_a_then_group_that_introduces_new_clips_still_counts_and_stays_met_when_seated` (existing) |
| 5e | Unnamed clips between the then rows and the last row (the "rest") make a then / first / last row misplaced | `U::test_described_sequence_reorders_attachment_order` (existing: an unnamed cafe clip sits between), `U::test_unnamed_clips_between_groups_do_not_fail_the_order` |
| 5f | Two first rows (first(A), first(B)) or two last rows judge each other as strays | `U::test_two_first_rows_and_two_last_rows_are_met_in_their_listed_order` |
| 5g | A clip named by two rows is counted against the wrong one (the first row that names it owns it) | `U::test_clip_named_twice_belongs_to_the_first_group` (existing), `U::test_a_then_row_naming_a_first_clip_again_is_judged_on_its_own_new_clips` |
| 6 | The planner still collapses a list into ONE row. The server cannot tell a list from a plain group ("the volleyball clips" has no listed order; no wording parsing, KRI-667), so the receipt stays "met". KNOWN GAP, not fixed here | `R::test_a_listed_order_collapsed_into_one_row_still_reads_met_known_gap` (`xfail(strict=True)`: flips to a failure, forcing a conscious edit, the day a structured list field exists) |
| 7 | The prompt change cannot be verified offline: it changes what the MODEL emits. The free replay evals only pin the schema / repair shapes, not model behaviour. Verified only by a live re-run of the thread (paid, not run here). The version bump is pinned by `tests/services/test_kri520_clip_intent_copy.py::test_prompt_versions_moved_with_the_new_prompt_tail` | not testable offline; stated in the PR |
| 8 | The KRI-723 / KRI-796 moments path (`index in moments`) is touched by the new rule | `tests/kria/test_kri723_moment_authority_m2.py`, `tests/kria/test_kri796_photo_between_moments.py`, `tests/kria/test_order_verdicts.py` (all existing, must stay green untouched) |
| 9 | The TR / EN wording of the receipt changes: codes stay `landed` / `misplaced`, status stays `met` / `partial`, reason stays "the clips did not end up there"; `brief_checks` is not edited | `C::test_five_listed_rows_out_of_place_are_misplaced_when_the_seating_rule_is_off` asserts the receipt text; `tests/kria/test_order_verdicts.py` (existing) |
| 10 | A wording regex or `"word" in text` test is added to read the list out of the creator's words | `tests/kria/test_kri667_no_new_intent_regexes.py` (ratchet, existing) |
| 11 | The render contract's pinned order and the planner's seating drift apart (the contract reads `apply_sequence` too) | `C::test_five_listed_rows_land_in_the_listed_order_from_any_attachment_order` asserts `plan.clip_ids == contract.order_ids` and `_verify(contract, _recipe(plan))` |
| 12 | The prod replay is not faithful (the recorded one-row strategy no longer reproduces prod's cut order, so the fix is judged against a different input) | `R::test_prods_recorded_one_row_strategy_reproduces_prods_cut_order` |
| 13 | An out-of-place row hides in the five-row replay: a swap in the final cut must read NOT met, naming the row that plays too early | `R::test_two_listed_rows_swapped_in_the_cut_read_not_met`, `R::test_the_recorded_cut_under_five_rows_is_judged_row_by_row` |
| 14 | A `status != resolved` or absent / contained / visual row now judged by the new rule | `C::test_a_group_with_no_clip_in_the_edit_is_reported_as_absent_not_misplaced` (existing), `C::test_an_unresolved_start_clip_is_reported_not_guessed` (existing) |
| 15 | A flag-off plan (`clip_intents_enabled=False`) starts emitting order outcomes | `U::test_flag_off_keeps_attachment_order_byte_identical` (existing) |
| 16 | A stricter first / last rule newly flags a plan that a LATER stage reorders (`editorial_order`, the approved plan's moment windows) and used to read vacuously met: that is the point (the receipt reads the finished cut), but it must not turn a render that was fine into a refusal | `tests/tasks/test_unified_montage_dispatch.py`, `tests/kria/test_kri723_moment_authority_m2.py`, `tests/kria/test_kri775_merope_m2_replay.py` (existing), plus the broad `tests/pipeline tests/services tests/kria` run |
| 17 | The new rule mis-reads a row whose clips were claimed by an earlier row (`owner` = the first row naming a clip, the rule `apply_sequence` seats by) | `U::test_a_then_row_naming_a_first_clip_again_is_judged_on_its_own_new_clips`, `U::test_two_first_rows_and_two_last_rows_are_met_in_their_listed_order` |

## Known gap (stated, not closed)

If the planner collapses a listed order into ONE position-less row, `apply_sequence` still seats
its members in attachment order and the receipt still reads `met`: from ids alone the server
cannot tell "the volleyball clips" (a group, no order) from "door, butcher, grill" (a list). Closing
that needs a model-emitted structured field (for example `listed: true` on the intent), not a
wording parser (KRI-667's ratchet forbids one).
