"""Offline selector failure matrix: no authority/candidate => zero calls; invalid model
output => unavailable; unknown provider outcome => retained liability, no retry;
valid allowlisted selection => candidate identity only, never editor operations.
"""

import json
from unittest.mock import Mock

import pytest

from app.agents._runtime import ModelInvocation, ProviderOutcomeUnknownError, RunContext
from app.services import bounded_review as meter
from app.services import editor_repair_reviewer as review


def context(**changes):
    retime = {
        "op": "retime_text_sequence",
        "target_ids": ["a", "b"],
        "start_s": 0,
        "end_s": 12,
        "fit_to_video": True,
    }
    return review.EditorRepairReviewContext(
        **(
            dict(
                original_request="Spread these exact words across the video",
                turn_id="turn",
                job_id="job",
                session_id="session",
                editor_state_digest="digest",
                editor_timing_only=True,
                planned_operations_json=json.dumps(
                    [retime, {"op": "rewrite_text", "text": "changed"}]
                ),
                candidate_id="preserve-copy",
                candidate_operations_json=json.dumps([retime]),
                evidence=(
                    review.EditorRepairEvidence(
                        evidence_id="copy-change",
                        source="compiler",
                        observation="Proposed rewrite changes copy",
                    ),
                ),
            )
            | changes
        )
    )


@pytest.fixture
def setup(monkeypatch):
    for field in (
        "creator_repair_reviewer_enabled",
        "credit_quality_review_enabled",
        "credit_quality_review_billing_verified",
        "ai_cost_control_enabled",
    ):
        monkeypatch.setattr(meter.settings, field, True)
    monkeypatch.setattr(meter.settings, "ai_usage_environment", "development")
    ledger = {}
    for name in (
        "reserve_paid_call",
        "mark_paid_call_started",
        "settle_paid_call",
        "mark_paid_call_unknown",
        "release_paid_call",
    ):
        ledger[name] = Mock(return_value=0.01 if name == "settle_paid_call" else object())
        monkeypatch.setattr(meter.costs, name, ledger[name])
    client = Mock()
    client.invoke.return_value = ModelInvocation(
        raw_text=json.dumps(
            dict(
                status="accept_candidate",
                candidate_id="preserve-copy",
                evidence_ids=["copy-change"],
                reason="Retain the requested timing and exact wording",
            )
        ),
        tokens_in=10,
        tokens_out=5,
    )
    ctx = RunContext(
        job_id="job",
        creator_agent_session_id="session",
        request_id="turn",
        reservation_approved=True,
        test_run_id="test",
        usage_purpose="live_eval",
        estimated_max_cost_usd=1,
    )
    return client, ctx, ledger


def test_valid_native_candidate_selection_preserves_operations(setup):
    client, ctx, ledger = setup
    original = context()
    result = review.review_editor_repair(original, run_context=ctx, client=client)
    assert result.status == "accept_candidate"
    assert result.candidate_id == "preserve-copy"
    assert result.provenance.cost_usd == 0.01
    assert "operations" not in result.model_dump()
    assert original == context()
    request = client.invoke.call_args.kwargs
    assert request["response_schema"]["additionalProperties"] is False
    assert "editor-state/compiler" in request["prompt"]
    assert "Gemini evidence" not in request["prompt"]
    client.invoke.assert_called_once()
    ledger["settle_paid_call"].assert_called_once()


@pytest.mark.parametrize(
    "changes",
    [
        {"editor_timing_only": False},
        {"candidate_id": None, "candidate_operations_json": None},
        {"candidate_operations_json": '[{"op":"retime_text_sequence","end_s":99}]'},
        {
            "planned_operations_json": '[{"op":"set_total_duration","target_s":12}]',
            "candidate_operations_json": '[{"op":"set_total_duration","target_s":12}]',
        },
    ],
)
def test_no_valid_candidate_never_calls_provider(setup, changes):
    client, ctx, ledger = setup
    result = review.review_editor_repair(context(**changes), run_context=ctx, client=client)
    assert result.status == "unavailable"
    client.invoke.assert_not_called()
    ledger["reserve_paid_call"].assert_not_called()


@pytest.mark.parametrize(
    "changes",
    [
        {"candidate_id": "invented"},
        {"evidence_ids": ["invented"]},
        {"evidence_ids": []},
        {"operations": [{"op": "rewrite_text"}]},
        {"suggested_wording": "different words"},
        {"start_s": 2},
    ],
)
def test_extra_or_ungrounded_model_output_is_rejected(setup, changes):
    client, ctx, _ = setup
    raw = json.loads(client.invoke.return_value.raw_text)
    client.invoke.return_value.raw_text = json.dumps(raw | changes)
    result = review.review_editor_repair(context(), run_context=ctx, client=client)
    assert result.status == "unavailable"
    assert result.reason == "invalid_response"


def test_unknown_liability_no_retry(setup):
    client, ctx, ledger = setup
    client.invoke.side_effect = ProviderOutcomeUnknownError("uncertain")
    result = review.review_editor_repair(context(), run_context=ctx, client=client)
    assert result.reason == "provider_outcome_unknown"
    assert result.provenance.cost_usd is None
    client.invoke.assert_called_once()
    ledger["mark_paid_call_unknown"].assert_called_once()
    ledger["release_paid_call"].assert_not_called()


def test_disabled_and_budget_denied_are_unavailable(setup, monkeypatch):
    client, ctx, ledger = setup
    monkeypatch.setattr(meter.settings, "creator_repair_reviewer_enabled", False)
    assert (
        review.review_editor_repair(context(), run_context=ctx, client=client).status
        == "unavailable"
    )
    client.invoke.assert_not_called()
    monkeypatch.setattr(meter.settings, "creator_repair_reviewer_enabled", True)
    ledger["reserve_paid_call"].side_effect = RuntimeError("budget denied")
    assert (
        review.review_editor_repair(context(), run_context=ctx, client=client).reason
        == "budget_denied"
    )
    client.invoke.assert_not_called()


@pytest.mark.parametrize("raw", ["not json", "{}", '{"status":"accept_candidate"}'])
def test_malformed_response_is_unavailable_without_retry(setup, raw):
    client, ctx, ledger = setup
    client.invoke.return_value.raw_text = raw
    assert review.review_editor_repair(context(), run_context=ctx, client=client).reason == (
        "invalid_response"
    )
    client.invoke.assert_called_once()
    ledger["settle_paid_call"].assert_called_once()


@pytest.mark.parametrize("status", ["keep_plan", "requires_confirmation", "unavailable"])
def test_nonselection_never_exposes_candidate(setup, status):
    client, ctx, _ = setup
    client.invoke.return_value.raw_text = json.dumps(
        dict(
            status=status, candidate_id=None, evidence_ids=[], reason="No safe correction selected"
        )
    )
    result = review.review_editor_repair(context(), run_context=ctx, client=client)
    assert result.status == status
    assert result.candidate_id is None


def test_invalid_original_timing_has_no_candidate(setup):
    client, ctx, ledger = setup
    original = json.loads(context().planned_operations_json)
    original[0]["end_s"] = -1
    candidate = [original[0]]
    result = review.review_editor_repair(
        context(
            planned_operations_json=json.dumps(original),
            candidate_operations_json=json.dumps(candidate),
        ),
        run_context=ctx,
        client=client,
    )
    assert result.reason == "no_eligible_candidate"
    client.invoke.assert_not_called()
    ledger["reserve_paid_call"].assert_not_called()


def test_incomplete_provider_output_cannot_select_candidate(setup):
    client, ctx, ledger = setup
    client.invoke.return_value.finish_status = "truncated"
    result = review.review_editor_repair(context(), run_context=ctx, client=client)
    assert result.reason == "provider_incomplete"
    assert result.candidate_id is None
    client.invoke.assert_called_once()


def test_other_turn_capability_cannot_authorize_review(setup):
    from dataclasses import replace

    client, ctx, ledger = setup
    result = review.review_editor_repair(
        context(), run_context=replace(ctx, request_id="other-turn"), client=client
    )
    assert result.reason == "unapproved_turn"
    client.invoke.assert_not_called()
    ledger["reserve_paid_call"].assert_not_called()


def test_candidate_json_must_retain_numeric_types_exactly(setup):
    client, ctx, ledger = setup
    original = json.loads(context().planned_operations_json)
    original[0]["end_s"] = 1
    candidate = json.loads(json.dumps([original[0]]))
    candidate[0]["end_s"] = True
    result = review.review_editor_repair(
        context(
            planned_operations_json=json.dumps(original),
            candidate_operations_json=json.dumps(candidate),
        ),
        run_context=ctx,
        client=client,
    )
    assert result.reason == "no_eligible_candidate"
    client.invoke.assert_not_called()
    ledger["reserve_paid_call"].assert_not_called()


@pytest.mark.parametrize("patch", [{"start_s": 1, "end_s": 3}, {"color": "#FF0000", "end_s": 3}])
def test_timing_patch_cannot_be_silently_discarded(setup, patch):
    client, ctx, ledger = setup
    original = json.loads(context().planned_operations_json)
    original[-1] = {"op": "patch_text", "target_ids": ["a"], "patch": patch}
    result = review.review_editor_repair(
        context(planned_operations_json=json.dumps(original)), run_context=ctx, client=client
    )
    assert result.reason == "no_eligible_candidate"
    client.invoke.assert_not_called()
    ledger["reserve_paid_call"].assert_not_called()
