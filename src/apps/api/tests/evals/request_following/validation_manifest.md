# KRI-459 validation manifest

This register keeps deterministic replay evidence separate from focused runtime coverage. None
of the rows below proves a render, device export, or paid live-model outcome.

| Journey | Executable evidence | Evidence level |
|---|---|---|
| Six distinct captions, then one correction only | `tests/evals/request_following/test_runner.py::test_v2_six_captions_then_one_correction_changes_only_the_target_label` | replay cassette through editor adapter/registry/compiler; in-memory compile only |
| Talking request on unsaved state | `tests/evals/request_following/test_runner.py::test_v2_unsupported_talking_and_slides_keep_unsaved_editor_state_and_reply_honestly` | replay cassette; verifies no-op and honest receipt only |
| Slides request on unsaved state | `tests/evals/request_following/test_runner.py::test_v2_unsupported_talking_and_slides_keep_unsaved_editor_state_and_reply_honestly` | replay cassette; verifies no-op and honest receipt only |
| Fifty clips interrupted/resumed and long request | `tests/services/test_clip_intent_planning_robustness.py::test_fifty_clip_long_prompt_over_cap_asks_one_focused_question`, `tests/services/test_clip_intent_planning_robustness.py::test_fifty_clip_long_prompt_within_cap_resolves_all` | focused planner tests |
| Cached answers after late analysis | `tests/kria/test_clip_answer_cache_postgres.py::test_cached_answers_keep_the_clip_record_and_survive_late_analysis` | Postgres-focused integration |
| Request/brief binding through later turns | `tests/kria/test_request_binding_journey.py` | runtime binding journey |
| Cooking words stay aligned to narrated steps | `tests/pipeline/test_narrated_alignment.py::test_align_script_high_confidence_covers_voiceover_duration`, `tests/agents/test_narrated_clip_alignment.py::test_valid_placements_round_trip` | focused deterministic timing and strict agent-schema tests; no render or paid-model claim |
| Lisbon creator-named place matching | `tests/agents/test_clip_request_resolver.py::test_parse_creator_text_label_keeps_match_whatever_its_value` | focused resolver parser test for a Lisbon/Pink Street label; does not prove unknown-location retrieval or a full journey |
| Revised football/volleyball/pub order after an alternative was rejected | `tests/evals/request_following/test_ordering_execution.py::test_revised_activity_order_constructs_shots_and_receipt_after_denied_alternative` | real `plan_unified_montage` construction plus requirement receipt; changed strategy inputs stand in for the prior rejection, with no DB denial or render claim |
| Missing Lisbon target stays unchecked despite matching text elsewhere | `tests/evals/request_following/test_ordering_execution.py::test_missing_lisbon_target_cannot_use_unrelated_text_as_clip_evidence` | deterministic brief-check target-evidence guard |
| Approved request remains pinned after a later brief message | `tests/kria/test_request_binding_journey.py::test_approval_dispatch_uses_pinned_request_after_later_brief` | Postgres-focused approval-dispatch test; checks the original request survives a later brief version |
| Media replacement invalidates the pinned approval | `tests/kria/test_request_binding_journey.py::test_media_generation_change_invalidates_pinned_approval` | Postgres-focused generation guard; claim is rejected after storage generation changes |
| Narration dependency / large media set | `tests/services/test_speech_montage_planning.py::test_fifty_assets_and_a_long_prompt_yield_multiple_grounded_excerpts` | focused planner test |
| Editor unsaved state | `tests/kria/test_editor_state_turns_postgres.py::test_speech_cut_with_unsaved_edits_is_an_honest_reply_not_a_crash` | Postgres-focused editor state test |

Lisbon missing/ambiguous-location retrieval, retry sequencing, and a complete later-message
request-following replay still need a reviewed cassette or production capture. The rows above
are focused contracts and binding checks, not a full journey, device export, or render proof.
A hand-authored reference must never be upgraded to actual execution or render proof.
