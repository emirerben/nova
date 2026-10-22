"""Real PostgreSQL: new text providers cannot bypass or duplicate reservations."""

from dataclasses import replace

import pytest
from sqlalchemy import text

from app.agents._runtime import ModelInvocation, ProviderOutcomeUnknownError
from app.database import sync_engine
from tests.agents.conftest import MockModelClient, SampleAgent, SampleInput
from tests.services.test_ai_cost_control import _request, ledger  # noqa: F401


@pytest.fixture
def provider_ledger(ledger):  # noqa: F811
    # Runtime keys are hashed; the shared fixture's prefix-key cleanup cannot
    # remove them. Clean by the attributed run id, including failed assertions.
    try:
        yield ledger
    finally:
        with sync_engine.begin() as db:
            db.execute(
                text("DELETE FROM ai_cost_reservations WHERE test_run_id=:id"),
                {"id": ledger + ":run"},
            )


@pytest.mark.parametrize(
    "model,provider", [("gpt-6.1-sol", "openai"), ("claude-sonnet-5-5", "anthropic")]
)
@pytest.mark.parametrize("unknown", [False, True])
def test_agent_run_new_provider_ledger(provider_ledger, model, provider, unknown):
    request = _request(provider_ledger, model, estimate=0.1)
    client = MockModelClient()
    response = (
        ProviderOutcomeUnknownError("outcome unknown")
        if unknown
        else ModelInvocation(raw_text='{"answer":"ok","score":90}', tokens_in=100, tokens_out=20)
    )
    client.queue(model, response)
    agent = SampleAgent(client)
    agent.spec = replace(
        agent.spec, model=model, fallback_models=("gemini-2.5-flash",), max_attempts=2
    )
    if unknown:
        with pytest.raises(ProviderOutcomeUnknownError):
            agent.run(SampleInput(topic="test"), ctx=request.ctx)
        with pytest.raises(ProviderOutcomeUnknownError):
            agent.run(SampleInput(topic="test"), ctx=request.ctx)
    else:
        agent.run(SampleInput(topic="test"), ctx=request.ctx)
    assert len(client.invocations) == 1
    with sync_engine.connect() as db:
        row = (
            db.execute(
                text(
                    "SELECT provider,status,settled_cost_usd FROM ai_cost_reservations "
                    "WHERE test_run_id = :id"
                ),
                {"id": request.ctx.test_run_id},
            )
            .mappings()
            .one()
        )
    assert row["provider"] == provider
    assert row["status"] == ("unknown" if unknown else "settled")
    if not unknown:
        assert float(row["settled_cost_usd"]) > 0


def test_envelope_caps_concurrent_distinct_batches(ledger):  # noqa: F811
    """A new batch id must not turn the approved total into another allowance."""
    import uuid
    from concurrent.futures import ThreadPoolExecutor

    from app.agents._runtime import AiBudgetExceededError
    from app.services.ai_cost_control import reserve_paid_call

    envelope = "comparison:" + str(uuid.uuid4())
    requests = []
    for i in range(2):
        request = _request(ledger, str(i), estimate=1.4, run_id=envelope + ":" + str(i))
        request.ctx.extra.update(
            paid_experiment_envelope_id=envelope, approved_envelope_max_usd=2.0
        )
        requests.append(request)

    def reserve(request):
        try:
            return reserve_paid_call(request)
        except AiBudgetExceededError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(reserve, requests))
    assert sum(r is not None for r in receipts) == 1


def test_unknown_submission_fences_the_entire_envelope(ledger):  # noqa: F811
    import uuid

    from app.services.ai_cost_control import (
        mark_paid_call_started,
        mark_paid_call_unknown,
        reserve_paid_call,
    )

    envelope = "comparison:" + str(uuid.uuid4())
    first = _request(ledger, "unknown-envelope", estimate=0.05, run_id=envelope + ":first")
    second = _request(ledger, "next-batch", estimate=0.05, run_id=envelope + ":second")
    for request in (first, second):
        request.ctx.extra.update(
            paid_experiment_envelope_id=envelope, approved_envelope_max_usd=2.0
        )
    receipt = reserve_paid_call(first)
    mark_paid_call_started(receipt)
    mark_paid_call_unknown(receipt)
    with pytest.raises(ProviderOutcomeUnknownError):
        reserve_paid_call(second)


def test_retained_unknown_allows_independent_case_but_never_duplicate(ledger):  # noqa: F811
    import uuid

    from app.services.ai_cost_control import (
        mark_paid_call_started,
        mark_paid_call_unknown,
        reconcile_unknown_liability,
        reserve_paid_call,
    )

    envelope = "comparison:" + str(uuid.uuid4())
    first = _request(ledger, "closed-unknown", estimate=0.05, run_id=envelope + ":first")
    second = _request(ledger, "independent", estimate=0.10, run_id=envelope + ":second")
    for request in (first, second):
        request.ctx.extra.update(
            paid_experiment_envelope_id=envelope, approved_envelope_max_usd=2.0
        )
    first.ctx.extra["comparison_case_id"] = "b" * 64
    second.ctx.extra["comparison_case_id"] = "c" * 64
    receipt = reserve_paid_call(first)
    mark_paid_call_started(receipt)
    mark_paid_call_unknown(receipt)
    evidence = {
        "provider_call_terminated": True,
        "maximum_call_cost_usd": 0.08,
        "sha256": "a" * 64,
        "protected_case_id": "b" * 64,
    }
    for invalid in (
        {**evidence, "provider_call_terminated": False},
        {**evidence, "maximum_call_cost_usd": 0.3},
        {**evidence, "sha256": "invalid"},
    ):
        with pytest.raises(ValueError):
            reconcile_unknown_liability(
                receipt.id,
                test_run_id=first.ctx.test_run_id,
                approved_batch_usd=0.2,
                evidence=invalid,
            )
    reconcile_unknown_liability(
        receipt.id,
        test_run_id=first.ctx.test_run_id,
        approved_batch_usd=0.2,
        evidence=evidence,
    )
    with pytest.raises(ProviderOutcomeUnknownError):
        reserve_paid_call(first)
    assert reserve_paid_call(second)
    cloned = _request(ledger, "clone-unknown", estimate=0.05, run_id=envelope + ":clone")
    cloned.ctx.extra.update(
        paid_experiment_envelope_id=envelope,
        approved_envelope_max_usd=2.0,
        comparison_case_id="b" * 64,
    )
    with pytest.raises(ProviderOutcomeUnknownError):
        reserve_paid_call(cloned)
    from app.agents._runtime import AiBudgetExceededError

    third = _request(ledger, "over-budget", estimate=1.8, run_id=envelope + ":third")
    third.ctx.extra.update(paid_experiment_envelope_id=envelope, approved_envelope_max_usd=2.0)
    with pytest.raises(ProviderOutcomeUnknownError):
        reserve_paid_call(third)  # missing case attribution cannot bypass the fence
    third.ctx.extra["comparison_case_id"] = "d" * 64
    with pytest.raises(AiBudgetExceededError):
        reserve_paid_call(third)
    with sync_engine.connect() as db:
        row = (
            db.execute(
                text(
                    "SELECT status,estimated_cost_usd,settled_cost_usd,usage_json "
                    "FROM ai_cost_reservations WHERE id=:id"
                ),
                {"id": receipt.id},
            )
            .mappings()
            .one()
        )
    assert row["status"] == "unknown"
    assert float(row["estimated_cost_usd"]) == 0.2
    assert row["settled_cost_usd"] is None
    assert row["usage_json"]["liability_reconciliation"]["actual_usage_known"] is False
