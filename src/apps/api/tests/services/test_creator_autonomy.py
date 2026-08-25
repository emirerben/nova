from app.services.creator_autonomy import build_auto_command, evaluate_auto_iteration


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
    assert evaluate_auto_iteration(
        _review(), opted_in=False, render_budget_remaining=1, automatic_revision_count=0
    ).reason_code == "session_opt_in_required"
    assert evaluate_auto_iteration(
        _review(), opted_in=True, render_budget_remaining=1, automatic_revision_count=1
    ).reason_code == "automatic_revision_cap_reached"
    assert evaluate_auto_iteration(
        _review(), opted_in=True, render_budget_remaining=0, automatic_revision_count=0
    ).reason_code == "render_budget_exhausted"


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
