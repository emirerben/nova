"""Failure matrix rows 11, 14, 15 and 42 on the real PostgreSQL ledger.

Spend must always be metered, capped by its own capability budget, counted while still
liable (reserved, started, unknown) and attributed to the review, never to evaluation.
"""

import uuid

import pytest
from sqlalchemy import text

from app.agents._runtime import (
    AiBudgetExceededError,
    AiCostControlPolicyError,
    ProviderOutcomeUnknownError,
    RunContext,
)
from app.config import settings
from app.database import sync_engine
from app.services.ai_cost_control import (
    PaidCallRequest,
    UsageMeter,
    estimate_call_cost_usd,
    mark_paid_call_started,
    mark_paid_call_unknown,
    reservation_for_key,
    reserve_paid_call,
    settle_paid_call,
)
from tests.services.test_ai_cost_control import _create_creator, ledger  # noqa: F401

pytestmark = pytest.mark.usefixtures("exclusive_intent_review_ledger")


@pytest.fixture
def review_ledger(ledger, monkeypatch):  # noqa: F811
    def clean():
        with sync_engine.begin() as db:
            db.execute(text("DELETE FROM ai_cost_reservations WHERE feature = 'intent_review'"))

    clean()
    monkeypatch.setattr(settings, "ai_usage_environment", "production")
    monkeypatch.setattr(settings, "ai_openai_intent_review_budget_usd", 0.20)
    yield ledger
    clean()


def request(prefix, suffix, *, estimate=0.05, creator=None, **context) -> PaidCallRequest:
    key = f"{prefix}:{suffix}"
    return PaidCallRequest(
        idempotency_key=key,
        feature="intent_review",
        model="gpt-6.1-sol",
        estimated_cost_usd=estimate,
        ctx=RunContext(
            request_id=key,
            request_id_authoritative=True,
            creator_id=str(creator) if creator else str(_create_creator(prefix, suffix)),
            **context,
        ),
        provider="openai",
        capability="intent_review",
    )


def test_worst_case_reservation_for_six_frames_is_priced_and_under_the_review_ceiling():
    estimate = estimate_call_cost_usd(
        model="gpt-6.1-sol",
        prompt="x" * 24_000,
        max_output_tokens=2_500,
        media_mime="image/jpeg",
        media_count=6,
        media_dims=[(640, 360)] * 6,
    )
    assert estimate == pytest.approx(0.077, abs=0.0005)
    assert estimate < settings.kria_intent_review_max_usd


def test_image_reservation_grows_with_real_dimensions_and_without_them_uses_the_ceiling():
    common = dict(
        model="gpt-6.1-sol",
        prompt="p",
        max_output_tokens=100,
        media_mime="image/jpeg",
        media_count=2,
    )
    small = estimate_call_cost_usd(media_dims=[(640, 360)] * 2, **common)
    unknown = estimate_call_cost_usd(**common)
    large = estimate_call_cost_usd(media_dims=[(2000, 2000)] * 2, **common)
    assert small == pytest.approx(unknown)
    assert large > small


def test_gemini_image_estimate_is_unchanged_by_the_openai_branch():
    gemini = estimate_call_cost_usd(
        model="gemini-3.1-pro-preview",
        prompt="p",
        max_output_tokens=100,
        media_mime="image/jpeg",
        media_count=2,
        media_dims=[(640, 360)] * 2,
    )
    assert gemini == estimate_call_cost_usd(
        model="gemini-3.1-pro-preview",
        prompt="p",
        max_output_tokens=100,
        media_mime="image/jpeg",
        media_count=2,
        media_width_px=640,
        media_height_px=360,
    )


def test_review_spend_is_metered_even_with_cost_control_disabled(review_ledger, monkeypatch):
    monkeypatch.setattr(settings, "ai_cost_control_enabled", False)
    assert reserve_paid_call(request(review_ledger, "metered")) is not None
    other = PaidCallRequest(
        idempotency_key=f"{review_ledger}:unrelated",
        feature="some_legacy_agent",
        model="gemini-3.1-pro-preview",
        estimated_cost_usd=0.05,
        ctx=RunContext(request_id="unrelated"),
    )
    assert reserve_paid_call(other) is None


def test_capability_budget_zero_rejects_every_reserve(review_ledger, monkeypatch):
    monkeypatch.setattr(settings, "ai_openai_intent_review_budget_usd", 0)
    with pytest.raises(AiBudgetExceededError) as caught:
        reserve_paid_call(request(review_ledger, "zero"))
    assert caught.value.scope == "provider:openai:capability:intent_review"


def test_capability_spend_counts_reserved_started_unknown_and_settled(review_ledger):
    first = request(review_ledger, "a", estimate=0.09)
    second = request(review_ledger, "b", estimate=0.09)
    receipt_a = reserve_paid_call(first)
    receipt_b = reserve_paid_call(second)
    with pytest.raises(AiBudgetExceededError):
        reserve_paid_call(request(review_ledger, "reserved-counts", estimate=0.09))

    mark_paid_call_started(receipt_a)
    mark_paid_call_started(receipt_b)
    mark_paid_call_unknown(receipt_b)
    settle_paid_call(receipt_a, UsageMeter(tokens_in=1000, tokens_out=500, resolved_model=None))
    # 0.0070 settled + 0.09 retained as unknown liability leaves room for exactly one more.
    assert reserve_paid_call(request(review_ledger, "c", estimate=0.09)) is not None
    with pytest.raises(AiBudgetExceededError):
        reserve_paid_call(request(review_ledger, "d", estimate=0.09))


def test_unknown_liability_counts_toward_the_cap_until_reconciled(review_ledger):
    creator = _create_creator(review_ledger, "held")
    held = reserve_paid_call(request(review_ledger, "held", estimate=0.15, creator=creator))
    mark_paid_call_started(held)
    mark_paid_call_unknown(held)
    with pytest.raises(AiBudgetExceededError):
        reserve_paid_call(request(review_ledger, "blocked", estimate=0.09))
    receipt, status = reservation_for_key(f"{review_ledger}:held")
    assert status == "unknown" and receipt.id == held.id
    with pytest.raises(ProviderOutcomeUnknownError):
        reserve_paid_call(request(review_ledger, "held", estimate=0.15, creator=creator))


def test_review_spend_is_not_evaluation_spend(review_ledger, monkeypatch):
    monkeypatch.setattr(settings, "ai_usage_environment", "development")
    monkeypatch.setattr(settings, "ai_openai_intent_review_budget_usd", 1.0)
    monkeypatch.setattr(settings, "ai_openai_evaluation_budget_usd", 0.6)
    run = f"{review_ledger}:run"
    qualification = request(
        review_ledger,
        "qualification",
        estimate=0.5,
        creator=None,
        usage_purpose="live_eval",
        test_run_id=run,
        estimated_max_cost_usd=2.0,
        reservation_approved=True,
    )
    assert reserve_paid_call(qualification) is not None
    evaluation = PaidCallRequest(
        idempotency_key=f"{review_ledger}:evaluation",
        feature="test_ai_cost_control:agent",
        model="gpt-6.1-sol",
        estimated_cost_usd=0.2,
        ctx=RunContext(
            request_id="evaluation",
            usage_purpose="live_eval",
            test_run_id=f"{review_ledger}:other-run",
            estimated_max_cost_usd=2.0,
            reservation_approved=True,
        ),
        provider="openai",
    )
    assert reserve_paid_call(evaluation) is not None


def test_internal_account_needs_canary_attribution_on_the_ledger(review_ledger):
    internal = _create_creator(review_ledger, "internal", internal=True)
    with pytest.raises(AiCostControlPolicyError) as caught:
        reserve_paid_call(request(review_ledger, "untagged", creator=internal))
    assert caught.value.reason == "release_canary_attribution_required"
    tagged = request(
        review_ledger,
        "tagged",
        creator=internal,
        usage_purpose="internal_canary",
        release_canary_id="intent-review-canary",
    )
    assert reserve_paid_call(tagged) is not None


def test_reservation_for_key_is_none_for_a_key_never_reserved(review_ledger):
    assert reservation_for_key(f"{review_ledger}:never") is None
    assert uuid.UUID(str(reserve_paid_call(request(review_ledger, "seen")).id))
    assert reservation_for_key(f"{review_ledger}:seen")[1] == "reserved"
