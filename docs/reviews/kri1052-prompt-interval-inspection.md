# Prompt-specific source interval inspection

This is a local implementation for KRI-1052, with activation off. It does not establish that the original Cabildo request succeeded or that a production agent watched the footage again.

## Acceptance and boundaries

A creator names actions within one take. Current indexed source intervals can narrow where to look. When stored evidence cannot locate an action, the planner inspects those bounded source intervals, preserves returned sample-relative timestamps on the source clock, and either pins the action or asks the existing moment-location question. An exterior view or a final photo cannot supply video-interior timestamp evidence.

The real path is `_gate_unresolved_choices` → `inspect_named_moments` → actor-owned current item/source/index validation → durable prompt-scoped inspection cache → `inspect_interval` → `SourceMomentLocationAgent` → `InspectedGroundings` → `named_moments.decide` → existing length plan → unified montage → `compile_execution_plan`. Provider transport is replayed in tests. No paid inspection or new customer-media upload ran during implementation.

Both `kria_prompt_moment_inspection_enabled` and `kria_prompt_moment_inspection_allow_provider` default to false. The first permits the planning integration. The second permits new provider work. Enabling the first alone only reads eligible cached localizer results. Activation and live-provider validation require a separate controlled rollout and budget scope.

## Source and answer authority

The planner requires a current actor-owned PlanItem, exact assignment path/generation/duration, an owned available immutable source, retained source hash, and current retained analysis/index versions. Indexed bounds and deterministic index IDs must agree with current facts. A generation HEAD check precedes evidence use and repeats after results. Missing library/source linkage fails closed into clarification.

Cache identity binds owner, source ID, generation, source hash, indexed fact version, sample bounds, requested action, complete named-moment context, localizer model and prompt version. A correction or re-upload invalidates reuse. Creator answers retain their existing question/generation binding and take priority. A current indexed grounding avoids unnecessary reinspection. Stale lexical groundings cannot bypass the current-source check.

The localizer distinguishes found, absent and uncertain. It returns sample-relative timestamped matches, never an edit. The server rejects invalid bounds, low confidence, missing evidence, missing sample hash and overly long matches. More than one occurrence or overlapping actions requires clarification. A unique match is accepted only after all eligible candidates have conclusive results. Model inspection remains uncertain evidence, with source/cache/sample provenance preserved in the pinned plan.

New provider work is capped at three calls under one 45-second deadline. Each sample is at most 20 seconds and its immutable source at most 64 MiB. Cache-only requests cannot acquire provider leases. A lease prevents overlapping identical requests. The planner does not call the collection HTTP endpoint or commit its transaction.

The timestamped localizer reuses the existing immutable-byte download, SHA check, bounded crop, short-lived File API upload and cleanup. No full-library reanalysis or global model activation is introduced.

## Offline evidence

- `tests/services/test_prompt_moment_inspection.py` covers cache/source/question/context changes, ambiguity, uncertain versus absent, stale results, current evidence reuse, no spend in cache-only mode and the three-call limit.
- `tests/integration/test_prompt_moment_inspection.py` uses disposable PostgreSQL, actual source/index/durable-cache queries and the real planner gate. It proves cold miss, replayed inspection, cache reuse, changed question, ownership rejection, generation replacement and exact timestamps reaching length-plan pins.
- `tests/integration/test_media_collections.py::test_real_targeted_read_crops_bytes_before_one_bounded_model_call` examines actual synthetic cropped frames before replayed upload, for both the existing factual agent and the new timestamped localizer.
- `tests/kria/test_prompt_moment_inspection_gate.py` exercises the real approval gate, unified montage planner and execution compiler. Six disjoint intervals of one asset retain their requested order. The compiled story timeline contains the exact source bounds. A changed approved interval is rejected. Dark switches, old clients, stale context and overlapping moments are controls.
- Existing Cabildo and creator-pointing regressions remain in the selected suite. The Cabildo fixture is sanitized retained incident shape, not missing original provider output. The timestamped localizer outputs are synthetic transport fixtures.

## Remaining gaps and rollback

There is no live-model generalization evidence, inspected new native export or production exposure claim. A typed timestamp returned by a replay is not independent visual proof that the original requested action occurs there. The retained native repeated-source capability probe is separate evidence and is not a new export of this change.

No indexed candidate interval means clarification. This implementation does not discover arbitrary action locations across an unindexed long take. More than three uncached candidate inspections can leave the request unresolved. It does not replace the original incident's missing raw provider outputs, fix every source-name misunderstanding, or publish the separate local explicit-order/None confirmation continuation `c27acc206`.

No migration or native recipe schema change is required. Existing non-inspection grounding and creator answer behavior remain available. Rollback is both switches off. Producers stay inactive until API/worker readers, cost scope and controlled visual acceptance are verified.
