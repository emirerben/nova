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
