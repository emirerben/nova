"""A continued turn's trace names every run, its true outcome and what each run really spent;
a finished turn leaves nothing of its checkpoint behind.

Failure modes, written before the code:

- the run the creator saw fail (the cap-reaching one) is the one run `planning_runs` lacks;
- `outcome` is a constant, so the trace cannot tell a run that continued from one that completed;
- the entries are only what a test wrote by hand: a real call or wait never reaches the column;
- research, the vision re-check and the script writer cost tens of seconds and are invisible;
- a completed or failed turn leaves its stored planner answers in Redis for 30 minutes.

Real `run_kria_turn` / `_continue_turn` / `_plan_with_live_agent` and the real Main Creator call
on Postgres and Redis; only the model client and the planner's own decisions are scripted.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest
import redis

from app.agents._schemas.creator_agent import (
    CapabilityAvailability,
    CreatorMediaRef,
    ResolvedCreatorManifest,
)
from app.agents.main_creator import MainCreatorAgent, MainCreatorInput
from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.kria import planner
from app.models import CreatorAgentTurn
from app.services.clip_intent_planning import wait_for_clip_understanding
from app.services.kria_trace import resolve_kria_trace
from app.tasks import kria_runtime
from app.tasks.kria_runtime import run_kria_turn
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project
from tests.kria.test_turn_auto_continue_postgres import (  # noqa: F401
    FINAL_REPLY,
    _publishes,
    _respond,
    _runtime_enabled,
    _script_planner,
    _submit,
)

_ASK = {
    "action": {
        "kind": "ask_user",
        "question": "Which clip should lead?",
        "reason_code": "choice",
        "options": [],
    }
}


def _hash_key(turn_id: str) -> str:
    return f"kria:turn-checkpoint:{turn_id}"


@pytest.fixture
def store():  # noqa: ANN201
    client = redis.from_url(settings.redis_url, socket_connect_timeout=1)
    try:
        client.ping()
    except redis.exceptions.ConnectionError as exc:
        pytest.skip(f"Redis not reachable: {exc!r}")
    seeded: list[str] = []
    yield SimpleNamespace(client=client, seeded=seeded)
    for key in seeded:
        client.delete(key)


def _runs(turn_id: str) -> list[dict]:
    with sync_session() as db:
        return list(db.get(CreatorAgentTurn, uuid.UUID(turn_id)).planning_runs or [])


def _leave_a_checkpoint(store, turn_id: str) -> None:  # noqa: ANN001
    key = _hash_key(turn_id)
    store.client.hset(key, "main_creator:digest", b'{"v": 1}')
    store.seeded.append(key)


@pytest.mark.asyncio
async def test_a_completed_turn_clears_its_checkpoint(monkeypatch, store) -> None:  # noqa: ANN001
    user_id, thread_id, _ = _seed_runtime_project()
    _script_planner(monkeypatch, _respond(FINAL_REPLY, continuable=False))
    accepted = await _submit(user_id, thread_id, "Start with the cooking clip.", 2)
    _leave_a_checkpoint(store, accepted.turn_id)

    result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)

    assert result["status"] == "completed"
    assert store.client.exists(_hash_key(accepted.turn_id)) == 0


@pytest.mark.asyncio
async def test_a_failed_turn_clears_its_checkpoint(monkeypatch, store) -> None:  # noqa: ANN001
    user_id, thread_id, _ = _seed_runtime_project()

    async def planner_fails(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("Kria could not produce a reliable editorial plan")

    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", planner_fails)
    accepted = await _submit(user_id, thread_id, "Start with the cooking clip.", 2)
    _leave_a_checkpoint(store, accepted.turn_id)

    with pytest.raises(RuntimeError):
        await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)

    assert store.client.exists(_hash_key(accepted.turn_id)) == 0


@pytest.mark.asyncio
async def test_a_continued_turn_keeps_its_checkpoint_for_the_next_run(
    monkeypatch,
    store,  # noqa: ANN001
) -> None:
    user_id, thread_id, _ = _seed_runtime_project()
    _script_planner(monkeypatch, replace(_respond("Checking.", continuable=True)))
    _publishes(monkeypatch)
    accepted = await _submit(user_id, thread_id, "Start with the cooking clip.", 2)
    _leave_a_checkpoint(store, accepted.turn_id)

    result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)

    assert result["status"] == "continued"
    assert store.client.exists(_hash_key(accepted.turn_id)) == 1


@pytest.mark.asyncio
async def test_the_run_that_reaches_the_cap_is_recorded_as_exhausted(monkeypatch) -> None:  # noqa: ANN001
    user_id, thread_id, _ = _seed_runtime_project()
    monkeypatch.setattr(settings, "kria_turn_auto_continue_max_runs", 2)
    pending = replace(
        _respond("Still checking.", continuable=True), continue_reason="clip_analysis_pending"
    )
    _script_planner(monkeypatch, pending)
    _publishes(monkeypatch)
    accepted = await _submit(user_id, thread_id, "Start with the cooking clip.", 2)

    first = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
    last = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)

    assert (first["status"], last["status"]) == ("continued", "completed")
    runs = _runs(accepted.turn_id)
    assert [(r["epoch"], r["outcome"], r["reason"]) for r in runs] == [
        (1, "continued", "clip_analysis_pending"),
        (2, "exhausted", "clip_analysis_pending"),
    ]


@pytest.mark.asyncio
async def test_the_run_that_finally_completes_a_continued_turn_is_recorded(monkeypatch) -> None:  # noqa: ANN001
    user_id, thread_id, _ = _seed_runtime_project()
    pending = replace(
        _respond("Checking.", continuable=True), continue_reason="clip_analysis_pending"
    )
    _script_planner(monkeypatch, pending, _respond(FINAL_REPLY, continuable=False))
    _publishes(monkeypatch)
    accepted = await _submit(user_id, thread_id, "Start with the cooking clip.", 2)

    await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
    await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)

    assert [(r["epoch"], r["outcome"]) for r in _runs(accepted.turn_id)] == [
        (1, "continued"),
        (2, "completed"),
    ]


@pytest.mark.asyncio
async def test_a_continued_turn_that_then_fails_records_the_failed_run(monkeypatch) -> None:  # noqa: ANN001
    user_id, thread_id, _ = _seed_runtime_project()
    pending = replace(_respond("Checking.", continuable=True), continue_reason="research_pending")
    calls = _script_planner(monkeypatch, pending)
    _publishes(monkeypatch)
    accepted = await _submit(user_id, thread_id, "Start with the cooking clip.", 2)
    await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)

    async def planner_fails(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("Kria could not produce a reliable editorial plan")

    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", planner_fails)
    with pytest.raises(RuntimeError):
        await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)

    assert len(calls) == 1
    assert [(r["epoch"], r["outcome"]) for r in _runs(accepted.turn_id)] == [
        (1, "continued"),
        (2, "failed"),
    ]


@pytest.mark.asyncio
async def test_an_ordinary_one_run_turn_writes_no_record(monkeypatch) -> None:  # noqa: ANN001
    user_id, thread_id, _ = _seed_runtime_project()
    _script_planner(monkeypatch, _respond(FINAL_REPLY, continuable=False))
    accepted = await _submit(user_id, thread_id, "Start with the cooking clip.", 2)

    await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)

    assert _runs(accepted.turn_id) == []


def _creator_input() -> MainCreatorInput:
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
    return MainCreatorInput(
        user_message="Start with the cooking clip.", capability_manifest=manifest
    )


@pytest.mark.asyncio
async def test_real_calls_waits_and_steps_reach_planning_runs(monkeypatch, store) -> None:  # noqa: ANN001
    """Nothing here writes a trace entry by hand: the real Main Creator call, the real
    clip-understanding wait, and the run's research step land in the column."""
    from tests.agents.conftest import MockModelClient  # noqa: PLC0415

    user_id, thread_id, _ = _seed_runtime_project()
    client = MockModelClient()
    monkeypatch.setattr(planner, "default_client", lambda: client)

    async def fake_plan_live_turn(db, *, thread_id, item_id, creator_id, **_kwargs):  # noqa: ANN001, ANN003
        await planner._call_main_creator(
            planner._CreatorInputs(
                agent_input=_creator_input(), intent_clips=[], creator_request=""
            ),
            thread_id=thread_id,
            creator_id=creator_id,
            item_id=item_id,
        )

        async def refresh():  # noqa: ANN202
            return []

        await wait_for_clip_understanding([], refresh=refresh, until=time.monotonic())
        return replace(
            _respond("Checking.", continuable=True), continue_reason="clip_analysis_pending"
        )

    def slow_research(planned, *_args, **_kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        time.sleep(0.06)
        return planned

    from app.kria import research_preview  # noqa: PLC0415

    monkeypatch.setattr(kria_runtime, "plan_live_turn", fake_plan_live_turn)
    monkeypatch.setattr(research_preview, "with_research", slow_research)
    _publishes(monkeypatch)
    client.queue(MainCreatorAgent.spec.model, _ASK)
    accepted = await _submit(user_id, thread_id, "Start with the cooking clip.", 2)
    store.seeded.append(_hash_key(accepted.turn_id))

    result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)

    assert result["status"] == "continued"
    calls = _runs(accepted.turn_id)[0]["calls"]
    assert [(c["name"], c["source"]) for c in calls] == [
        ("main_creator", "model"),
        ("clip_understanding_wait", "wait"),
        ("research", "step"),
    ]
    assert calls[2]["ms"] >= 50
    async with AsyncSessionLocal() as db:
        trace = await resolve_kria_trace(db, turn_id=uuid.UUID(accepted.turn_id))
    assert trace["turns"][0]["planning_runs"][0]["calls"] == calls
