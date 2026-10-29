"""Unknown deadline liabilities must survive lighter calls and concurrent turns.

Failure modes: releasing a timeout, reusing a reservation, bypassing a shared
cap, and estimating the old output limit. PR runtime checks run only when its
exact checkout is explicitly selected; provider timing is simulated.
"""

from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.agents._runtime import AiBudgetExceededError, RunContext
from app.config import settings
from app.database import sync_engine
from app.services import ai_cost_control as costs

PR_HEAD = "05ea27c29a918973ba20a4c58006fcc56a5e8749"


@pytest.fixture
def deadline_ledger(monkeypatch):
    run_id = f"deadline-credit:{uuid.uuid4()}"
    monkeypatch.setattr(settings, "ai_cost_control_enabled", True)
    monkeypatch.setattr(settings, "ai_usage_environment", "development")
    monkeypatch.setattr(settings, "ai_paid_test_attribution_required", True)
    monkeypatch.setattr(settings, "ai_development_monthly_budget_usd", 1000.0)
    with sync_engine.connect() as db:
        db.execute(text("SELECT 1 FROM ai_cost_reservations LIMIT 0"))
    yield run_id
    with sync_engine.begin() as db:
        db.execute(
            text("DELETE FROM ai_cost_reservations WHERE test_run_id LIKE :prefix"),
            {"prefix": run_id + "%"},
        )


def _context(run_id):
    return RunContext(
        request_id=run_id,
        usage_purpose="live_eval",
        test_run_id=run_id,
        estimated_max_cost_usd=2.0,
        reservation_approved=True,
        extra={"skip_agent_run_persist": True},
    )


def _request(run_id, rung, estimate=0.2):
    return costs.PaidCallRequest(
        idempotency_key=f"{run_id}:{rung}",
        feature="deadline_compatibility",
        model="gemini-3.1-pro-preview" if rung < 3 else "gemini-3.6-flash",
        estimated_cost_usd=estimate,
        ctx=_context(run_id),
    )


def _spend():
    with sync_engine.connect() as db:
        return costs._scope_spend(
            db, environment="development", period_start=costs._month_start(datetime.now(UTC))
        )


def _rows(run_id):
    with sync_engine.connect() as db:
        return (
            db.execute(
                text(
                    "SELECT id,status,estimated_cost_usd,settled_cost_usd FROM "
                    "ai_cost_reservations WHERE test_run_id=:run ORDER BY created_at"
                ),
                {"run": run_id},
            )
            .mappings()
            .all()
        )


def test_three_distinct_reservations_keep_two_unknown_liabilities(deadline_ledger):
    before = _spend()
    for rung in (1, 2, 3):
        receipt = costs.reserve_paid_call(_request(deadline_ledger, rung))
        costs.mark_paid_call_started(receipt)
        if rung < 3:
            costs.mark_paid_call_unknown(receipt)
        else:
            costs.settle_paid_call_cost(receipt, cost_usd=0.01, resolved_model="gemini-3.6-flash")
    rows = _rows(deadline_ledger)
    assert len({row["id"] for row in rows}) == 3
    assert [row["status"] for row in rows] == ["unknown", "unknown", "settled"]
    assert _spend() - before == Decimal("0.41")


def test_unknown_liability_exhausts_budget_before_next_contact(deadline_ledger, monkeypatch):
    before = _spend()
    monkeypatch.setattr(
        settings,
        "ai_development_monthly_budget_usd",
        float((before + Decimal("0.3")) / Decimal("0.8")),
    )
    contacts = []
    receipt = costs.reserve_paid_call(_request(deadline_ledger, 1))
    costs.mark_paid_call_started(receipt)
    contacts.append(1)
    costs.mark_paid_call_unknown(receipt)
    with pytest.raises(AiBudgetExceededError):
        costs.reserve_paid_call(_request(deadline_ledger, 2))
        contacts.append(2)
    assert contacts == [1]
    assert [row["status"] for row in _rows(deadline_ledger)] == ["unknown"]


def test_concurrent_turns_share_environment_cap(deadline_ledger, monkeypatch):
    before = _spend()
    monkeypatch.setattr(
        settings,
        "ai_development_monthly_budget_usd",
        float((before + Decimal("0.4")) / Decimal("0.8")),
    )

    def submit(turn):
        try:
            return costs.reserve_paid_call(_request(f"{deadline_ledger}:{turn}", 1, 0.3))
        except AiBudgetExceededError:
            return None

    with ThreadPoolExecutor(max_workers=2) as workers:
        receipts = list(workers.map(submit, [1, 2]))
    assert sum(receipt is not None for receipt in receipts) == 1
    assert _spend() - before == Decimal("0.3")


def test_doubled_copilot_output_budget_increases_reservation():
    estimates = [
        costs.estimate_call_cost_usd(
            model="gemini-3.1-pro-preview",
            prompt="Edit the approved timeline.",
            max_output_tokens=cap,
            media_mime=None,
        )
        for cap in (8192, 16384)
    ]
    assert estimates[1] > estimates[0]
    assert estimates[1] - estimates[0] == pytest.approx(2 * 8192 * 12 / 1_000_000)


@pytest.mark.skipif(
    os.getenv("NOVA_DEADLINE_PR_HEAD") != PR_HEAD, reason="requires exact PR 1934 runtime checkout"
)
@pytest.mark.parametrize("exhaust_cap", [False, True])
def test_actual_pr_ladder_uses_real_ledger(deadline_ledger, monkeypatch, exhaust_cap):
    import json
    from types import SimpleNamespace

    from app.agents import _runtime as runtime
    from app.agents.edit_copilot import EditCopilotAgent
    from app.agents.main_creator import MainCreatorAgent
    from tests.agents.test_deadline_ladder import _FIXTURE, _Clock, _Provider

    assert EditCopilotAgent.max_output_tokens == 16384
    clock = _Clock()
    monkeypatch.setattr(
        runtime, "time", SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep)
    )
    fixture = json.loads(_FIXTURE.read_text())
    provider = _Provider(clock, fixture["raw_text"], {"high": None, "medium": None, "low": 12.0})
    agent = MainCreatorAgent(provider)
    ctx = _context(deadline_ledger)
    ctx.deadline_monotonic = clock.now + 180
    before = _spend()
    if exhaust_cap:
        prompt = agent.render_prompt(agent.Input.model_validate(fixture["input"]))
        estimate = costs.estimate_call_cost_usd(
            model=agent.spec.model,
            prompt=prompt,
            max_output_tokens=agent.max_output_tokens,
            media_mime=None,
        )
        monkeypatch.setattr(
            settings,
            "ai_development_monthly_budget_usd",
            float((before + Decimal(str(estimate * 1.5))) / Decimal("0.8")),
        )
        with pytest.raises(AiBudgetExceededError):
            agent.run(fixture["input"], ctx=ctx)
        assert len(provider.calls) == 1
        assert [row["status"] for row in _rows(deadline_ledger)] == ["unknown"]
    else:
        agent.run(fixture["input"], ctx=ctx)
        assert [call["thinking_level"] for call in provider.calls] == ["high", "medium", "low"]
        assert [call["timeout_s"] for call in provider.calls] == [105, 50, 25]
        rows = _rows(deadline_ledger)
        assert len({row["id"] for row in rows}) == 3
        assert [row["status"] for row in rows] == ["unknown", "unknown", "settled"]
        assert _spend() - before == sum(
            row["estimated_cost_usd"] if row["status"] == "unknown" else row["settled_cost_usd"]
            for row in rows
        )
