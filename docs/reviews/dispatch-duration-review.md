# Dispatch duration review acceptance

This change adds a read-only audit and regression harness. It does not install a production hook, call a model, repair settings, or replace Yasin's KRI-1071 recognizer fix.

## Authority and result

`capture_dispatch_duration` copies the validated frozen BriefBinding, its creator request and scoped requirements, approved plan, final persisted creator strategy, rebuilt contract, Job ID and generation. Immutable JSON strings prevent later Job mutation from changing the captured evidence. The snapshot digest binds a proposal to these exact inputs.

`review_dispatch_duration` derives whole-output authority only from live global timing requirements and approved choice answers through `creator_duration(brief=..., authoritative_answers=...)`. It never passes creator text to that helper. Clip-scoped holds remain in the captured binding and never become global targets. Conflicting global values require clarification. Relevant approved choice values are checked before numeric conversion. Boolean, nonnumeric, nonfinite, nonpositive or conflicting values require clarification with no candidate, including disagreement with a live global requirement. Consistent ceilings remain ceilings. An inconsistent ceiling requires clarification and never proposes an exact-target repair.

Results are `consistent`, `correction_required`, `no_explicit_global`, or `clarification_needed`. A correction carries only the server candidate `restore_approved_total_duration`, a global target, the binding digest and the captured-state digest. It has no executor. It cannot rewrite wording, flatten scopes, edit source intervals, or claim a repair succeeded.

`consistent` without `recipe_checked` means only the captured settings agree. A supplied typed recipe is checked within one 30fps frame of an exact total. Ceilings use the existing duration-bound policy, including explicit strict export caps. This is a consistency check, not authorization or proof that this recipe belongs to a particular execution. The caller must validate recipe identity, binding, ownership, freshness and source association before relying on it. Published MP4 duration and scoped-shot fulfillment remain separate evidence.

## Required production boundaries, not installed here

1. In `tasks/content_plan_build.py::_dispatch_item_render`, capture after `build_generative_job`, the BriefBinding contract rebuild and `stamp_length_policy`, before Job commit/enqueue. The factory strips incoming duration provenance and re-derives it from the request, so an earlier planner-only review misses the regression.
2. In `tasks/generative_build.py::_run_phone_guided_job`, re-audit after all compile/repair/sound candidates have settled and final `validate_phone_pilot_recipe`, before `make_device_request`/`pin_device_request`. Use a fresh final-settings capture plus the final recipe. Never treat an old dispatch audit as a current admission receipt.
3. Any future provider work runs outside DB locks under an explicitly supplied priced capability. Reacquire and compare Job/generation, approved draft/binding, source identity and recipe digest before persisting a proposal or admitting a render. This module does not implement those fences.
4. A correction must pass existing approval and compile paths, then both audits again. Reintroduced bad recognition at the factory must still produce `correction_required`. The original parser fix remains necessary to avoid repeated overwrite or refusal.

## Proven by tests

`tests/kria/test_dispatch_duration_review.py` uses the existing KRI-788 harness. `_dispatch_item_render`, `build_generative_job`, BriefBinding resolution and contract reconstruction are real. DB queries and enqueue are mocked. A factory recognizer fixture models either corrected output or the old 3/None corruption. Correct-output fixtures are not qualification of the unavailable KRI-1071 branch.

The matrix covers scoped shot3/global20, scoped hold2/global15, quoted numbers, no explicit global duration, conflicting globals, pre-factory correction overwritten downstream, mismatched bound authority and fresh capture after later mutation. The scoped hold carries the existing server `authority=explicit` stamp; an unstamped clip duration intentionally has legacy semantics elsewhere in the codebase.

A separate native fixture exercises real parser, editor compiler and `prepare_editor_commit`, producing a pinned recipe with a three-second first source, twenty-second total and preserved source order. The audit also rejects a real twelve-second recipe despite consistent dispatch settings. This is component integration across two fixtures, not the actual runtime dispatch-to-native-worker journey or a same-execution provenance proof.

## Still pending

Production hook installation, locked identity checks, runtime-worker journey, normal approval of a correction, exported-byte measurement, OpenAI or Gemini judgment, provider budget admission and live/native UI qualification. No paid call, browser, simulator or UI automation is part of this work.
