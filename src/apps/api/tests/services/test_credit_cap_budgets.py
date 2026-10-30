"""Credit-cap failures: bypass, races, attribution loss, unknown retry and expiry.

Exercise the actual PostgreSQL authority without contacting any provider.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.agents._runtime import (
    AiBudgetExceededError,
    AiCostControlPolicyError,
    ProviderOutcomeUnknownError,
)
from app.config import settings
from app.database import sync_engine
from app.services.ai_cost_control import (
    mark_paid_call_started,
    mark_paid_call_unknown,
    reserve_paid_call,
    settle_paid_call_cost,
)
from tests.services.test_ai_cost_control import _request
from tests.services.test_ai_cost_control import ledger as ledger


@pytest.fixture
def credit(ledger, monkeypatch):
    monkeypatch.setattr(settings, "ai_usage_environment", "development")
    monkeypatch.setattr(settings, "generated_images_enabled", True)
    monkeypatch.setattr(settings, "generated_narration_enabled", True)
    for name in (
        "ai_openai_monthly_budget_usd",
        "ai_anthropic_monthly_budget_usd",
        "ai_openai_image_budget_usd",
        "ai_openai_narration_budget_usd",
        "ai_openai_evaluation_budget_usd",
        "ai_anthropic_evaluation_budget_usd",
    ):
        monkeypatch.setattr(settings, name, 10.0)
    return ledger


def image(credit, suffix, feature="generated_image", **kwargs):
    return replace(
        _request(credit, suffix, estimate=0.06, feature=feature, run_id=f"{credit}:{suffix}"),
        provider="openai",
        model="image-model",
        **kwargs,
    )


def test_generated_disabled_even_when_global_enforcement_off(credit, monkeypatch):
    monkeypatch.setattr(settings, "ai_cost_control_enabled", False)
    monkeypatch.setattr(settings, "generated_images_enabled", False)
    with pytest.raises(AiCostControlPolicyError):
        reserve_paid_call(image(credit, "disabled"))


@pytest.mark.parametrize("cap", ["ai_openai_monthly_budget_usd", "ai_openai_image_budget_usd"])
def test_zero_budget_fails_closed_with_global_switch_off(credit, monkeypatch, cap):
    monkeypatch.setattr(settings, "ai_cost_control_enabled", False)
    monkeypatch.setattr(settings, cap, 0.0)
    with pytest.raises(AiBudgetExceededError):
        reserve_paid_call(image(credit, "zero"))


@pytest.mark.parametrize("cap", ["ai_openai_monthly_budget_usd", "ai_openai_image_budget_usd"])
def test_concurrent_image_and_edit_share_caps(credit, monkeypatch, cap):
    monkeypatch.setattr(settings, cap, 0.1)
    requests = [image(credit, "one"), image(credit, "two", feature="generated_image_edit")]

    def attempt(request):
        try:
            reserve_paid_call(request)
            return "reserved"
        except AiBudgetExceededError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, requests)) == ["rejected", "reserved"]


def test_unknown_expired_liability_counts_and_retry_is_fenced(credit, monkeypatch):
    monkeypatch.setattr(settings, "ai_openai_image_budget_usd", 0.1)
    request = image(credit, "unknown")
    receipt = reserve_paid_call(request)
    mark_paid_call_started(receipt)
    mark_paid_call_unknown(receipt)
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE ai_cost_reservations SET expires_at=:at WHERE id=:id"),
            {"at": datetime.now(UTC) - timedelta(days=1), "id": receipt.id},
        )
    with pytest.raises(ProviderOutcomeUnknownError):
        reserve_paid_call(request)
    with pytest.raises(AiBudgetExceededError):
        reserve_paid_call(image(credit, "new"))


def test_settlement_preserves_capability_and_uses_actual_cost(credit, monkeypatch):
    monkeypatch.setattr(settings, "ai_openai_evaluation_budget_usd", 0.1)
    request = image(credit, "evaluation", feature="any_text_eval", capability="evaluation")
    receipt = reserve_paid_call(request)
    mark_paid_call_started(receipt)
    settle_paid_call_cost(
        receipt,
        cost_usd=0.03,
        resolved_model="text-model",
        usage_json={"_budget_capability": "image"},
    )
    reserve_paid_call(
        replace(
            request,
            idempotency_key=f"{credit}:next",
            ctx=_request(credit, "next", estimate=0.06).ctx,
        )
    )
    with pytest.raises(AiBudgetExceededError):
        reserve_paid_call(replace(request, idempotency_key=f"{credit}:blocked"))


def test_generated_capability_cannot_be_mislabeled(credit):
    with pytest.raises(AiCostControlPolicyError):
        reserve_paid_call(image(credit, "spoof", capability="evaluation"))


def test_provider_cap_combines_capabilities_across_environments(credit, monkeypatch):
    monkeypatch.setattr(settings, "ai_openai_monthly_budget_usd", 0.1)
    reserve_paid_call(image(credit, "development-image"))
    monkeypatch.setattr(settings, "ai_usage_environment", "lab")
    with pytest.raises(AiBudgetExceededError) as caught:
        reserve_paid_call(image(credit, "lab-narration", feature="generated_narration"))
    assert caught.value.scope == "provider:openai"


def test_evaluation_cap_is_separate_from_image_and_other_provider(credit, monkeypatch):
    monkeypatch.setattr(settings, "ai_openai_image_budget_usd", 0.06)
    monkeypatch.setattr(settings, "ai_openai_evaluation_budget_usd", 0.06)
    monkeypatch.setattr(settings, "ai_anthropic_evaluation_budget_usd", 0.06)
    reserve_paid_call(image(credit, "image"))
    evaluation = image(credit, "eval", feature="text_eval", capability="evaluation")
    reserve_paid_call(evaluation)
    reserve_paid_call(
        replace(evaluation, provider="anthropic", idempotency_key=f"{credit}:anthropic")
    )
    with pytest.raises(AiBudgetExceededError) as caught:
        reserve_paid_call(replace(evaluation, idempotency_key=f"{credit}:eval-blocked"))
    assert caught.value.scope == "provider:openai:capability:evaluation"


def test_expired_unstarted_generated_call_cannot_start(credit):
    receipt = reserve_paid_call(image(credit, "expired"))
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE ai_cost_reservations SET expires_at=:at WHERE id=:id"),
            {"at": datetime.now(UTC) - timedelta(seconds=1), "id": receipt.id},
        )
    with pytest.raises(ProviderOutcomeUnknownError):
        mark_paid_call_started(receipt)


def test_released_attempt_can_reserve_again_without_double_count(credit, monkeypatch):
    from app.services.ai_cost_control import release_paid_call

    monkeypatch.setattr(settings, "ai_openai_image_budget_usd", 0.06)
    request = image(credit, "retry")
    first = reserve_paid_call(request)
    release_paid_call(first)
    second = reserve_paid_call(request)
    assert second.id == first.id
    with pytest.raises(AiBudgetExceededError):
        reserve_paid_call(image(credit, "second-call"))


def test_ledger_unavailable_cannot_bypass_generated_enforcement(credit, monkeypatch):
    from app.agents._runtime import CostControlUnavailableError

    monkeypatch.setattr(settings, "ai_cost_control_enabled", False)

    class BrokenEngine:
        def begin(self):
            raise RuntimeError("ledger unavailable")

    monkeypatch.setattr("app.database.sync_engine", BrokenEngine())
    with pytest.raises(CostControlUnavailableError):
        reserve_paid_call(image(credit, "unavailable"))


def test_unknown_and_settlement_wait_for_provider_budget_lock(credit):
    from concurrent.futures import TimeoutError
    from threading import Event

    receipt = reserve_paid_call(image(credit, "locked"))
    mark_paid_call_started(receipt)
    for mutation in (
        lambda: mark_paid_call_unknown(receipt),
        lambda: settle_paid_call_cost(receipt, cost_usd=0.03, resolved_model="image-model"),
    ):
        started = Event()

        def attempt():
            started.set()
            mutation()

        with ThreadPoolExecutor(max_workers=1) as pool:
            with sync_engine.begin() as conn:
                conn.execute(
                    text(
                        "SELECT pg_advisory_xact_lock("
                        "hashtextextended('credit-provider:openai', 0))"
                    )
                )
                future = pool.submit(attempt)
                assert started.wait(timeout=2)
                with pytest.raises(TimeoutError):
                    future.result(timeout=0.1)
            future.result(timeout=2)


def test_waiting_start_cannot_use_expiry_from_transaction_start(credit):
    from concurrent.futures import TimeoutError
    from threading import Event

    receipt = reserve_paid_call(image(credit, "expires-while-waiting"))
    started = Event()

    def attempt():
        started.set()
        mark_paid_call_started(receipt)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with sync_engine.begin() as conn:
            conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended('credit-provider:openai', 0))")
            )
            future = pool.submit(attempt)
            assert started.wait(timeout=2)
            with pytest.raises(TimeoutError):
                future.result(timeout=0.1)
            conn.execute(
                text("UPDATE ai_cost_reservations SET expires_at=clock_timestamp() WHERE id=:id"),
                {"id": receipt.id},
            )
        with pytest.raises(ProviderOutcomeUnknownError):
            future.result(timeout=2)


@pytest.mark.parametrize(
    "provider,model", [("openai", "gpt-6.1-sol"), ("anthropic", "claude-sonnet-5-5")]
)
@pytest.mark.parametrize("purpose", ["live_eval", "provider_smoke"])
def test_text_evaluation_cannot_omit_capability_to_escape_cap(
    credit, monkeypatch, provider, model, purpose
):
    monkeypatch.setattr(settings, f"ai_{provider}_evaluation_budget_usd", 0.0)
    request = replace(
        _request(credit, "text-eval", estimate=0.01, purpose=purpose),
        provider=provider,
        model=model,
    )
    with pytest.raises(AiBudgetExceededError) as caught:
        reserve_paid_call(request)
    assert caught.value.scope == f"provider:{provider}:capability:evaluation"
