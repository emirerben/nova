"""GET /admin/agents/report against real PostgreSQL rows, through the real route.

Failure modes the numbers must survive (each has a test):

  - a run older than the window leaks into the totals
  - percentiles averaged or taken over the wrong rows (p50 / p95 are checked by hand)
  - `ok_fallback` counted as a failure, or not counted as a fallback
  - an outcome outside SUCCESS_OUTCOMES (terminal, schema_invalid) not counted as a failure
  - an owner-less run (eval, background) invisible: it must be persisted and counted
  - one agent's rows merged across models, so a fallback model's cost hides under the primary
  - a settled ledger row for a feature with no agent_run (captions) missing from the cross-check
  - chat-turn buckets: `proposed` is applied; `stale` is neither unsupported nor failed
  - no token, or a wrong token, reads the report

The agent names, models and features are unique to the test, so rows other tests left on the
shared database never enter an assertion.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.agents._persistence import persist_agent_run
from app.agents._runtime import SUCCESS_OUTCOMES
from app.config import settings
from app.database import engine as async_engine
from app.database import sync_engine
from app.main import app
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

TOKEN = "test-admin-token"


@pytest.fixture
def seeded(monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", TOKEN)
    tag = uuid.uuid4().hex[:8]
    user_id, thread_id, session_id = _seed_runtime_project()
    ids = {
        "tag": tag,
        "agent": f"test.fleet.{tag}",
        "owner_less_agent": f"test.fleet.unowned.{tag}",
        "primary": f"primary-{tag}",
        "fallback": f"fallback-{tag}",
        "chat_model": f"chat-{tag}",
        "feature": f"caption_test_{tag}",
        "session_id": session_id,
    }
    with sync_engine.begin() as conn:
        plan_item_id = conn.execute(
            text("SELECT active_plan_item_id FROM creation_threads WHERE id = :t"),
            {"t": str(thread_id)},
        ).scalar_one()
        grant_id = uuid.uuid4()
        conn.execute(
            text(
                "INSERT INTO internal_account_grants "
                "(id, creator_id, status, granted_by, idempotency_key) "
                "VALUES (:id, :c, 'active', 'pytest', :k)"
            ),
            {"id": str(grant_id), "c": str(user_id), "k": f"fleet-{tag}"},
        )
    ids.update(user_id=user_id, plan_item_id=plan_item_id, grant_id=grant_id)
    yield ids
    with sync_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM agent_run WHERE agent_name LIKE :p"), {"p": f"test.fleet.%{tag}"}
        )
        conn.execute(
            text("DELETE FROM ai_cost_reservations WHERE feature = :f"), {"f": ids["feature"]}
        )
        conn.execute(
            text("DELETE FROM edit_interaction_receipts WHERE model = :m"), {"m": ids["chat_model"]}
        )
        conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": str(user_id)})


def _agent_run(ids, *, agent, model, outcome, latency_ms, cost, days_ago=0, owned=True):
    row_id = uuid.uuid4()
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_run (id, creator_agent_session_id, owner_label, agent_name, "
                "prompt_version, model, outcome, attempts, latency_ms, settled_cost_usd, "
                "environment, created_at) VALUES (:id, :s, :o, :a, 'v1', :m, :out, 1, :l, :c, "
                "'production', :t)"
            ),
            {
                "id": str(row_id),
                "s": str(ids["session_id"]) if owned else None,
                "o": None if owned else f"eval:{ids['tag']}",
                "a": agent,
                "m": model,
                "out": outcome,
                "l": latency_ms,
                "c": cost,
                "t": datetime.now(UTC) - timedelta(days=days_ago, minutes=1),
            },
        )


def _receipt(ids, outcome):
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO edit_interaction_receipts (id, event_kind, creator_id, plan_item_id, "
                "variant_id, utterance, inferred_intent, model_reply, eligibility_basis, "
                "internal_grant_id, proposed_operations, proposed_operations_digest, "
                "prompt_version, model, proposal_outcome, rejection_reasons) VALUES "
                "(:id, 'proposal', :c, :p, 'v', 'u', 'edit', 'r', 'internal_grant', :g, '[]', "
                ":d, 'pv', :m, :o, '[]')"
            ),
            {
                "id": str(uuid.uuid4()),
                "c": str(ids["user_id"]),
                "p": str(ids["plan_item_id"]),
                "g": str(ids["grant_id"]),
                "d": uuid.uuid4().hex,
                "m": ids["chat_model"],
                "o": outcome,
            },
        )


def _reservation(ids, *, status, settled):
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO ai_cost_reservations (id, idempotency_key, environment, "
                "usage_purpose, feature, provider, requested_model, status, estimated_cost_usd, "
                "settled_cost_usd, price_version, period_start, expires_at) VALUES "
                "(:id, :k, 'production', 'customer', :f, 'openai', 'gpt-4o', :s, 0.05, :c, 'v', "
                "now(), now() + interval '1 hour')"
            ),
            {
                "id": str(uuid.uuid4()),
                "k": uuid.uuid4().hex,
                "f": ids["feature"],
                "s": status,
                "c": settled,
            },
        )


def _get(path="/admin/agents/report?days=14", token=TOKEN):
    headers = {"X-Admin-Token": token} if token else {}
    try:
        with TestClient(app) as client:
            return client.get(path, headers=headers)
    finally:
        # Each TestClient owns an event loop; an asyncpg connection stays bound to the one
        # that opened it, so the next request must not reuse this pool.
        async_engine.sync_engine.dispose(close=False)


def test_the_report_needs_the_admin_token(seeded):
    assert _get(token=None).status_code == 422
    assert _get(token="wrong").status_code == 401


def test_per_agent_and_model_numbers_match_the_seeded_rows(seeded):
    ids = seeded
    latencies = range(100, 1100, 100)
    outcomes = ["ok"] * 8 + ["terminal", "schema_invalid"]
    for latency, outcome in zip(latencies, outcomes, strict=True):
        _agent_run(
            ids,
            agent=ids["agent"],
            model=ids["primary"],
            outcome=outcome,
            latency_ms=latency,
            cost=0.01,
        )
    for outcome in ["ok", "ok", "ok", "ok_fallback"]:
        _agent_run(
            ids,
            agent=ids["agent"],
            model=ids["fallback"],
            outcome=outcome,
            latency_ms=50,
            cost=0.002,
        )
    _agent_run(
        ids,
        agent=ids["agent"],
        model=ids["primary"],
        outcome="terminal",
        latency_ms=9_999,
        cost=7.0,
        days_ago=30,
    )

    body = _get().json()

    rows = {(r["agent_name"], r["model"]): r for r in body["agents"]}
    primary = rows[(ids["agent"], ids["primary"])]
    assert primary["calls"] == 10
    assert primary["settled_cost_usd"] == pytest.approx(0.10)
    assert primary["latency_p50_ms"] == pytest.approx(550.0)
    assert primary["latency_p95_ms"] == pytest.approx(955.0)
    assert primary["failed_calls"] == 2 and primary["failed_share"] == pytest.approx(0.2)
    assert primary["fallback_calls"] == 0
    assert primary["outcomes"] == {"ok": 8, "schema_invalid": 1, "terminal": 1}
    fallback = rows[(ids["agent"], ids["fallback"])]
    assert fallback["calls"] == 4 and fallback["failed_calls"] == 0
    assert fallback["fallback_calls"] == 1 and fallback["fallback_share"] == pytest.approx(0.25)
    assert fallback["settled_cost_usd"] == pytest.approx(0.008)
    assert body["success_outcomes"] == sorted(SUCCESS_OUTCOMES)


def test_a_wider_window_admits_the_old_run(seeded):
    ids = seeded
    _agent_run(ids, agent=ids["agent"], model=ids["primary"], outcome="ok", latency_ms=1, cost=0.5)
    _agent_run(
        ids,
        agent=ids["agent"],
        model=ids["primary"],
        outcome="ok",
        latency_ms=1,
        cost=0.5,
        days_ago=30,
    )

    two_weeks = _get("/admin/agents/report?days=14").json()
    ninety = _get("/admin/agents/report?days=90").json()

    pick = lambda body: next(r for r in body["agents"] if r["agent_name"] == ids["agent"])  # noqa: E731
    assert pick(two_weeks)["calls"] == 1
    assert pick(ninety)["calls"] == 2
    assert two_weeks["window_exceeds_retention"] is False
    assert ninety["window_exceeds_retention"] is True


def test_an_owner_less_run_is_persisted_and_counted(seeded):
    ids = seeded
    persist_agent_run(
        job_id=f"eval:{ids['tag']}",
        segment_idx=None,
        agent_name=ids["owner_less_agent"],
        prompt_version="v1",
        model=ids["primary"],
        outcome="ok",
        attempts=1,
        tokens_in=1,
        tokens_out=1,
        settled_cost_usd=0.25,
        cost_usd=0.25,
        latency_ms=40,
        input_dict=None,
        output_dict=None,
        raw_text=None,
    )

    body = _get().json()

    row = next(r for r in body["agents"] if r["agent_name"] == ids["owner_less_agent"])
    assert row["calls"] == 1 and row["settled_cost_usd"] == pytest.approx(0.25)
    assert body["unowned_runs"]["by_agent"][ids["owner_less_agent"]] == 1
    assert body["unowned_runs"]["total"] >= 1


def test_a_per_job_reader_never_sees_an_owner_less_run(seeded):
    ids = seeded
    _agent_run(
        ids,
        agent=ids["agent"],
        model=ids["primary"],
        outcome="ok",
        latency_ms=1,
        cost=0.1,
        owned=False,
    )

    with sync_engine.connect() as conn:
        per_job = conn.execute(
            text("SELECT count(*) FROM agent_run WHERE job_id IS NOT NULL AND agent_name = :a"),
            {"a": ids["agent"]},
        ).scalar_one()

    assert per_job == 0


def test_the_ledger_section_counts_calls_that_have_no_agent_run(seeded):
    ids = seeded
    _reservation(ids, status="settled", settled=0.03)
    _reservation(ids, status="settled", settled=0.02)
    _reservation(ids, status="released", settled=None)
    _reservation(ids, status="unknown", settled=None)

    body = _get().json()

    row = next(r for r in body["ledger"] if r["feature"] == ids["feature"])
    assert (row["provider"], row["reservations"], row["settled"]) == ("openai", 4, 2)
    assert (row["released"], row["unknown"]) == (1, 1)
    assert row["settled_cost_usd"] == pytest.approx(0.05)
    assert row["unsettled_estimate_usd"] == pytest.approx(0.05)
    assert body["ledger_settled_by_provider"]["openai"] >= 0.05


def test_chat_turn_outcomes_bucket_proposed_as_applied_and_keep_stale_apart(seeded):
    ids = seeded
    for outcome in [
        "proposed",
        "proposed",
        "applied",
        "clarification",
        "unsupported",
        "unsupported",
        "failed",
        "stale",
        "no_effect",
    ]:
        _receipt(ids, outcome)

    chat = _get().json()["chat_turns"]

    row = next(r for r in chat["by_model"] if r["model"] == ids["chat_model"])
    assert row == {
        "model": ids["chat_model"],
        "total": 9,
        "applied": 3,
        "clarification": 1,
        "unsupported": 2,
        "failed": 1,
        "other": 2,
    }
    assert "training-eligible" in chat["scope"]
