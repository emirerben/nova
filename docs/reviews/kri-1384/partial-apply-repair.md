# KRI-1386 and KRI-1387: partial apply and the repair loop

## Request and acceptance

A creator asks for several changes in one chat message. Before this change, one op with a
bad value made the whole turn a clarification, so every valid sibling was dropped, and an op
the compiler refused ended the turn with "I can't do that on this edit. Nothing was changed."

The change is accepted when these hold:

- A valid op applies even when a sibling op is rejected.
- Every rejected op the model could fix goes back to it once per repair round, with why it
  failed and what would fix it. A limit inside an open family is named, not re-asked. Kept ops
  are not re-asked.
- An op that no round fixes is named in the reply and in `unmet_requests`.
- A bulk media op and `patch_text_appearance` stay atomic.
- No repair call starts when one copilot call does not fit before the turn deadline.

## Failure modes, written before the code

Each mode has a case in `src/apps/api/tests/kria/test_copilot_partial_apply_repair.py`.

| Id | Failure mode | Case |
| --- | --- | --- |
| F1 | A style applies without its timing pair, and the reply says both happened. | `test_top_left_mistake_is_repaired_and_all_three_apply` |
| F2 | A bulk op is split, so half of "every image" applies. | `test_atomic_bundle_never_applies_in_part[bulk]` |
| F3 | A `patch_text_appearance` sibling applies without the appearance change. | `test_atomic_bundle_never_applies_in_part[text_appearance]`, `test_atomic_bundle_goes_back_whole_for_repair` |
| F4 | The reply claims an op that was rejected. | `test_three_asks_one_unfixable_two_apply_and_the_third_is_named`, `test_no_repair_without_time_for_a_call` |
| F5 | Dropping a sibling shifts what a later index points at. | `test_dropping_a_sibling_never_shifts_a_later_index` |
| F6 | A repair call starts with too little turn time left. | `test_no_repair_without_time_for_a_call` |
| F7 | A repair answer drops a rejected op without fixing or naming it. | `test_stale_index_the_repair_ignores_is_named_after_two_rounds` |
| F8 | A failed repair call fails the whole turn. | `test_failed_repair_call_keeps_the_partial_edit` |
| F9 | An unsure repair answer throws the kept ops away. | `test_unsure_repair_answer_keeps_the_kept_ops` |
| F10 | An op the parser accepts but the compiler refuses sinks every sibling. | `test_compile_refusal_reaches_the_model_and_is_repaired` |
| F11 | A text-timing conflict, which is a creator decision, becomes a refusal. | `test_text_timing_conflict_passes_through` |

## Negative controls

Each mutant below undoes one part of the change. The replay file fails under every mutant
(offline replay, run on the PR head before commit).

| Mutant | Failing cases |
| --- | --- |
| `invalid_value()` lowers confidence to 0.4 again | 6 |
| no repair round | 8 |
| no pre-compile check | 1 |
| rejected ops not named in `unmet_requests` | 4 |
| turn deadline ignored | 4 |

## Evidence level

Offline replay only. The provider transport is scripted. No live model, device or production
evidence. The request-following replay report is unchanged from
`docs/reviews/kri-185/request-following-baseline.md` (44% met, 4 overclaims), because no
recorded thread has a rejected sibling. Live repair quality waits for the KRI-1389 baseline.
