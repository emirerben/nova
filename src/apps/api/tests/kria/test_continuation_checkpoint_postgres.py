"""A continued turn reuses what its first run already paid for, and says why it continued.

PR 1 made a later run's planner calls NEW paid calls so the ledger no longer refuses them.
That left every continuation re-paying (and re-waiting for) the Main Creator answer it already
had: ~$0.14 and ~40 s per extra run in prod thread b61b7673. Failure modes, written before the
code:

- crash after the provider settled but before the answer was stored: nothing is stored, the
  next run misses and calls under its own epoch-scoped key (PR 1), so it pays again but is
  never refused or stranded;
- two workers on one turn: the lease epoch fences writes of the turn; a late write from the
  older run is for the same (turn, call, input) so it holds the same answer;
- the prompt changed between runs (clip analysis landed, the creator's request or brief moved,
  a new thread event): the key holds a digest of the call's whole input, so it misses;
- the run cap: the last permitted run neither reads nor writes, and the TTL bounds the rest;
- the stored answer no longer parses (schema drift after a deploy): a miss, never an error;
- the store (Redis) is down or slow: a miss and a skipped write, never a failed turn;
- the first run never reads: only a run that is itself a continuation can have a checkpoint.

Real Postgres ledger, real MainCreatorAgent, real Redis; only the model client is fake.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
import redis
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, ProgrammingError

from app.agents._schemas.creator_agent import (
    CapabilityAvailability,
    CreatorMediaRef,
    ResolvedCreatorManifest,
)
from app.agents.main_creator import MainCreatorAgent, MainCreatorInput
from app.config import settings
from app.database import sync_engine
from app.kria import planner
from app.kria.contracts import KriaTurnPlan
from app.services import ai_cost_control
from app.tasks import kria_runtime

_ASK = {
    "action": {
        "kind": "ask_user",
        "question": "Which clip should lead?",
        "reason_code": "choice",
        "options": [],
    }
}


@pytest.fixture
def ledger(monkeypatch: pytest.MonkeyPatch):
    creator_id = uuid.uuid4()
    try:
        with sync_engine.connect() as conn:
            conn.execute(text("SELECT 1 FROM ai_cost_reservations LIMIT 0"))
        redis.from_url(settings.redis_url, socket_connect_timeout=1).ping()
    except (OperationalError, redis.exceptions.ConnectionError) as exc:
        pytest.skip(f"Postgres or Redis not reachable: {exc!r}")
    except ProgrammingError as exc:
        pytest.fail(f"database was not migrated to the cost-control schema: {exc!r}")
    monkeypatch.setattr(settings, "ai_cost_control_enabled", True)
    monkeypatch.setattr(settings, "ai_usage_environment", "production")
    monkeypatch.setattr(settings, "ai_production_monthly_budget_usd", 1_000.0)
    with sync_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:id, :email)"),
            {"id": str(creator_id), "email": f"checkpoint-{creator_id}@example.test"},
        )
    yield creator_id
    with sync_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM ai_cost_reservations WHERE creator_id = :id"), {"id": str(creator_id)}
        )
        conn.execute(text("DELETE FROM users WHERE id = :id"), {"id": str(creator_id)})


def _main_creator_input(message: str) -> MainCreatorInput:
    available = CapabilityAvailability(available=True)
    manifest = ResolvedCreatorManifest(
        item_id="item-1",
        edit_format="montage",
        render_program="guided",
        media=[CreatorMediaRef(media_id="clip-01", kind="video")],
        capabilities={"edit_format:montage": available},
        context_hash="a" * 64,
        manifest_hash="b" * 64,
    )
    return MainCreatorInput(user_message=message, capability_manifest=manifest)


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch, ledger):
    """``run(epoch, message=...)`` plans one run of ONE turn through the real call sites."""

    from tests.agents.conftest import MockModelClient  # noqa: PLC0415

    client = MockModelClient()
    monkeypatch.setattr(planner, "default_client", lambda: client)
    reservations: list[Any] = []
    real_reserve = ai_cost_control.reserve_paid_call

    def recording_reserve(request):  # noqa: ANN001, ANN202
        reservations.append(request)
        return real_reserve(request)

    monkeypatch.setattr(ai_cost_control, "reserve_paid_call", recording_reserve)
    answers: list[Any] = []
    state = {"message": "Start with the cooking clip."}

    async def fake_plan_live_turn(db, *, thread_id, item_id, creator_id, **_kwargs):  # noqa: ANN001, ANN003
        answers.append(
            await planner._call_main_creator(
                planner._CreatorInputs(
                    agent_input=_main_creator_input(state["message"]),
                    intent_clips=[],
                    creator_request="",
                ),
                thread_id=thread_id,
                creator_id=creator_id,
                item_id=item_id,
            )
        )
        return SimpleNamespace(
            plan=KriaTurnPlan(mode="respond", turn_value="recovery", response="still checking")
        )

    monkeypatch.setattr(kria_runtime, "plan_live_turn", fake_plan_live_turn)
    snapshot = {
        "thread_id": str(uuid.uuid4()),
        "item_id": str(uuid.uuid4()),
        "creator_id": str(ledger),
        "creator_agent_session_id": None,
    }
    turn_id = uuid.uuid4()

    async def run(epoch: int, message: str | None = None) -> None:
        if message is not None:
            state["message"] = message
        client.queue(MainCreatorAgent.spec.model, _ASK)
        await kria_runtime._plan_with_live_agent(
            snapshot,
            state["message"],
            turn_id=turn_id,
            lease_owner="worker",
            lease_epoch=epoch,
        )

    harness_ = SimpleNamespace(
        run=run, client=client, reservations=reservations, answers=answers, turn_id=turn_id
    )
    yield harness_
    store = redis.from_url(settings.redis_url)
    for key in store.scan_iter(f"kria:turn-checkpoint:{turn_id}:*"):
        store.delete(key)


async def test_a_continued_run_reuses_the_first_runs_main_creator_answer(harness) -> None:
    await harness.run(1)
    await harness.run(2)

    assert len(harness.client.invocations) == 1
    assert len(harness.reservations) == 1
    assert harness.answers[1] == harness.answers[0]


async def test_a_changed_request_between_runs_calls_the_model_again(harness) -> None:
    await harness.run(1)
    await harness.run(2, message="Start with the beach clip instead.")

    assert len(harness.client.invocations) == 2
    assert len(harness.reservations) == 2


async def test_the_first_run_never_reads_a_checkpoint(harness) -> None:
    await harness.run(2)  # leaves an answer behind
    await harness.run(1)

    assert len(harness.client.invocations) == 2


async def test_the_last_permitted_run_stores_nothing(harness, monkeypatch) -> None:
    monkeypatch.setattr(settings, "kria_turn_auto_continue_max_runs", 2)

    await harness.run(2)

    store = redis.from_url(settings.redis_url)
    assert list(store.scan_iter(f"kria:turn-checkpoint:{harness.turn_id}:*")) == []


async def test_an_unreadable_checkpoint_is_a_miss_not_an_error(harness) -> None:
    await harness.run(1)
    store = redis.from_url(settings.redis_url)
    for key in store.scan_iter(f"kria:turn-checkpoint:{harness.turn_id}:*"):
        store.set(key, b"{not json")

    await harness.run(2)

    assert len(harness.client.invocations) == 2


async def test_a_dead_store_never_fails_the_turn(harness, monkeypatch) -> None:
    from app.services import turn_checkpoint  # noqa: PLC0415

    def down():  # noqa: ANN202
        raise redis.exceptions.ConnectionError("redis is down")

    monkeypatch.setattr(turn_checkpoint, "_redis", down)

    await harness.run(1)
    await harness.run(2)

    assert len(harness.client.invocations) == 2
