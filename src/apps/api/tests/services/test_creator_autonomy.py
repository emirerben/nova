from app.services.creator_autonomy import (
    build_auto_bundle,
    build_auto_command,
    evaluate_auto_iteration,
    recover_auto_bundle,
)
from app.services.creator_sessions import serialize_session


def _review(**overrides):
    value = {
        "status": "complete",
        "review_mode": "objective",
        "render_generation_id": "generation-1",
        "confidence": 0.9,
        "quality_score": 3.0,
        "expected_improvement": 0.5,
        "objective_tag": "objective_quality",
        "allowlist_action": "caption_legibility",
        "proposed_revision": {"revision_id": "revision-1", "summary": "Fix legibility"},
    }
    value.update(overrides)
    return value


def test_auto_policy_requires_all_thresholds_and_objective_tag():
    decision = evaluate_auto_iteration(
        _review(confidence=0.84),
        opted_in=True,
        render_budget_remaining=1,
        automatic_revision_count=0,
    )
    assert decision.decision == "skip"
    assert decision.reason_code == "confidence_below_threshold"

    decision = evaluate_auto_iteration(
        _review(expected_improvement=0.49),
        opted_in=True,
        render_budget_remaining=1,
        automatic_revision_count=0,
    )
    assert decision.reason_code == "expected_improvement_below_threshold"

    decision = evaluate_auto_iteration(
        _review(objective_tag="taste"),
        opted_in=True,
        render_budget_remaining=1,
        automatic_revision_count=0,
    )
    assert decision.reason_code == "objective_tag_required"


def test_auto_policy_is_one_cycle_and_kill_switch_is_session_opt_in():
    assert (
        evaluate_auto_iteration(
            _review(), opted_in=False, render_budget_remaining=1, automatic_revision_count=0
        ).reason_code
        == "session_opt_in_required"
    )
    assert (
        evaluate_auto_iteration(
            _review(), opted_in=True, render_budget_remaining=1, automatic_revision_count=1
        ).reason_code
        == "automatic_revision_cap_reached"
    )
    assert (
        evaluate_auto_iteration(
            _review(), opted_in=True, render_budget_remaining=0, automatic_revision_count=0
        ).reason_code
        == "render_budget_exhausted"
    )


def test_auto_policy_accepts_only_allowlisted_objective_revision():
    decision = evaluate_auto_iteration(
        _review(), opted_in=True, render_budget_remaining=1, automatic_revision_count=0
    )
    assert decision.decision == "eligible"
    assert decision.command is None  # command is built only after exact variant fencing

    blocked = evaluate_auto_iteration(
        _review(allowlist_action="change_audio_strategy"),
        opted_in=True,
        render_budget_remaining=1,
        automatic_revision_count=0,
    )
    assert blocked.reason_code == "treatment_not_allowlisted"


def test_auto_command_is_pinned_and_removes_only_existing_optional_treatment():
    pin = {
        "expected_manifest_hash": "a" * 64,
        "expected_context_hash": "b" * 64,
        "expected_job_id": "job-1",
        "expected_variant_id": "variant-1",
        "expected_generation_id": "generation-1",
        "expected_revision": 2,
        "expected_ownership_epoch": 4,
    }
    command = build_auto_command(
        "remove_optional_treatment",
        pin=pin,
        review={"treatment": "sfx", "treatment_id": "sfx-1"},
        variant={"sound_effects": [{"id": "sfx-1", "src_gcs_path": "catalog/sfx.wav"}]},
    )
    assert command.command == "remove_optional_treatment"
    assert command.treatment == "sfx"
    assert command.treatment_id == "sfx-1"
    assert command.expected_generation_id == "generation-1"


def test_auto_command_never_guesses_transition_or_optional_treatment():
    pin = {
        "expected_manifest_hash": "a" * 64,
        "expected_context_hash": "b" * 64,
        "expected_job_id": "job-1",
        "expected_variant_id": "variant-1",
        "expected_generation_id": "generation-1",
        "expected_revision": 2,
        "expected_ownership_epoch": 4,
    }
    try:
        build_auto_command(
            "transition_fallback",
            pin=pin,
            review={},
            variant={"ai_timeline": {"slots": [{}, {}]}},
        )
    except ValueError as exc:
        assert str(exc) == "reviewed_boundary_required"
    else:
        raise AssertionError("missing reviewed transition boundary must fail closed")

    try:
        build_auto_command(
            "remove_optional_treatment",
            pin=pin,
            review={"treatment": "sfx"},
            variant={"sound_effects": [{"id": "sfx-1"}]},
        )
    except ValueError as exc:
        assert str(exc) == "reviewed_treatment_required"
    else:
        raise AssertionError("missing reviewed treatment id must fail closed")


def test_auto_bundle_pins_post_opt_in_revision_and_recovers_exact_request():
    pin = {
        "expected_manifest_hash": "a" * 64,
        "expected_context_hash": "b" * 64,
        "expected_job_id": "job-1",
        "expected_variant_id": "variant-1",
        "expected_generation_id": "generation-1",
        "expected_revision": 8,
        "expected_ownership_epoch": 4,
    }
    bundle = build_auto_bundle(
        session_id="session-1",
        idempotency_key="creator-auto:session-1:generation-1",
        pin=pin,
        action="caption_legibility",
        review=_review(),
        variant={},
    )
    recovered = recover_auto_bundle(
        bundle.model_dump(mode="json"),
        session_id="session-1",
        idempotency_key="creator-auto:session-1:generation-1",
        job_id="job-1",
        variant_id="variant-1",
        generation_id="generation-1",
        ownership_epoch=4,
    )
    assert recovered.model_dump(mode="json") == bundle.model_dump(mode="json")
    assert recovered.expected_revision == 8

    try:
        recover_auto_bundle(
            bundle.model_dump(mode="json"),
            session_id="session-1",
            idempotency_key="creator-auto:session-1:generation-1",
            job_id="job-1",
            variant_id="variant-1",
            generation_id="mutated-generation",
            ownership_epoch=4,
        )
    except ValueError as exc:
        assert str(exc) == "automatic_revision_receipt_stale"
    else:
        raise AssertionError("recovery must reject a changed generation")


def test_public_auto_receipt_is_bounded_and_excludes_rollback_plan_and_bundle():
    from types import SimpleNamespace

    session = SimpleNamespace(
        id="session-1",
        phase="awaiting_feedback",
        revision=8,
        render_attempts=2,
        max_render_attempts=2,
        active_plan=None,
        target_job_id="job-1",
        last_review={
            "status": "complete",
            "auto_iteration": {
                "status": "queued",
                "bundle": {"commands": [{"asset_id": "must-not-leak"}]},
                "rollback_receipt": {
                    "job_id": "job-1",
                    "variant_id": "variant-1",
                    "previous_generation_id": "generation-1",
                    "craft_receipt_id": "receipt-1",
                    "previous_assembly_plan": {"media": "must-not-leak"},
                },
            },
        },
        events=[],
        created_at=None,
        updated_at=None,
    )
    response = serialize_session(session)
    auto = response["last_review"]["auto_iteration"]
    assert "bundle" not in auto
    assert auto["rollback_receipt"] == {
        "job_id": "job-1",
        "variant_id": "variant-1",
        "previous_generation_id": "generation-1",
        "craft_receipt_id": "receipt-1",
    }
