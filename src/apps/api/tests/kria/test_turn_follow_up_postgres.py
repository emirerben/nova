"""Replays of the chat-turn bugs TLC found in `formal/kria_turns` (real Postgres).

Each test drives the entry points a client reaches (`submit_turn`, `cancel_turn`) and the
worker task (`run_kria_turn`, `reconcile_kria_turns`) through one counterexample, step for
step, and asserts what the creator sees: whether a message is accepted, and what a reply was
planned from. Only the planner is stubbed; its stub reads history through the planner's own
`_load_thread_events`, as every real planning step does.
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.database import sync_session
from app.kria import planner, runtime
from app.kria.api_schemas import SubmitTurnBody, TurnAccepted
from app.kria.contracts import KriaTurnPlan
from app.kria.planner import PlannedKriaTurn
from app.kria.runtime import cancel_turn, submit_turn
from app.models import CreationThread, CreatorAgentTurn
from app.tasks import kria_runtime as rt
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

_db_name = make_url(settings.database_url).database or ""
if not _db_name.endswith("_test"):
    pytest.skip(f"refusing to write to non-test database {_db_name!r}", allow_module_level=True)
try:
    with sync_session() as _probe:
        _probe.execute(text("select 1"))
except OperationalError:
    pytest.skip("nova_test Postgres not reachable", allow_module_level=True)


@pytest.fixture(autouse=True)
def _runtime_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)


async def _send(user_id: uuid.UUID, thread_id: uuid.UUID, message: str) -> TurnAccepted:
    """The app sends a message at the thread revision it last read."""
    # Unpooled: called from the test's loop and from inside the worker's `asyncio.run`.
    engine = create_async_engine(settings.asyncpg_database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            revision = int((await db.get(CreationThread, thread_id)).revision)
            await db.rollback()
            accepted, _ = await submit_turn(
                db,
                thread_id=thread_id,
                creator_id=user_id,
                body=SubmitTurnBody(
                    message=message,
                    client_event_id=f"msg-{uuid.uuid4().hex}",
                    expected_thread_revision=revision,
                ),
            )
            return accepted
    finally:
        await engine.dispose()


async def _cancel(user_id: uuid.UUID, thread_id: uuid.UUID, turn_id: str) -> None:
    engine = create_async_engine(settings.asyncpg_database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            revision = int((await db.get(CreationThread, thread_id)).revision)
            await db.rollback()
            await cancel_turn(
                db,
                thread_id=thread_id,
                turn_id=uuid.UUID(turn_id),
                creator_id=user_id,
                expected_thread_revision=revision,
            )
    finally:
        await engine.dispose()


async def _history_turn_ids(thread_id: uuid.UUID) -> list[str]:
    """Which messages (by turn id) the planner's own history load hands the model."""
    engine = create_async_engine(settings.asyncpg_database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            events = await planner._load_thread_events(db, thread_id)  # noqa: SLF001
    finally:
        await engine.dispose()
    return [str(payload.get("turn_id")) for role, payload in events if role == "user"]


class _WorkerDied(BaseException):
    """A killed process: no `except Exception` handler in the task runs after it."""


def _status(turn_id: str) -> str:
    with sync_session() as db:
        return db.get(CreatorAgentTurn, uuid.UUID(turn_id)).status


def _reply(text_: str) -> PlannedKriaTurn:
    return PlannedKriaTurn(
        plan=KriaTurnPlan(mode="respond", turn_value="question", response=text_),
        manifest_hash="a" * 64,
        context_hash="b" * 64,
    )


def _sweep() -> None:
    """One reconciler pass. Other tests' rows share the database, so no broker I/O."""
    with (
        patch.object(rt.run_kria_turn, "apply_async"),
        patch.object(rt.execute_kria_approval, "apply_async"),
        patch.object(rt, "_settle_dispatched_executions", return_value=0),
    ):
        rt._reconcile_kria_turns_body()  # noqa: SLF001 - the advisory lock is shared by xdist


@pytest.mark.asyncio
async def test_a_follow_up_is_not_stuck_when_cancel_dies_before_promoting(monkeypatch) -> None:  # noqa: ANN001
    """TLC NoStuckFollowUp: Submit, Submit, Cancel, ApiCrash, then Kria is settled.

    The API commits the cancel, then dies before its second transaction promotes the
    follow-up. Nothing else ever promotes it, so every later message gets 409
    "A follow-up is already waiting".
    """
    user_id, thread_id, _ = _seed_runtime_project()
    first = await _send(user_id, thread_id, "make the intro faster")
    follow_up = await _send(user_id, thread_id, "and use a calmer song")
    assert (first.status, follow_up.status) == ("pending", "queued")

    async def _api_dies(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        raise ConnectionError("API process died after the cancel committed")

    monkeypatch.setattr(runtime, "_promote_queued_successor", _api_dies)
    with pytest.raises(ConnectionError):
        await _cancel(user_id, thread_id, first.turn_id)
    assert _status(first.turn_id) == "cancelled"

    _sweep()

    # Pending is what the sweep republishes (a parallel worker's sweep may get there first).
    assert _status(follow_up.turn_id) == "pending"
    later = await _send(user_id, thread_id, "also trim the ending")  # no 409
    assert later.status == "queued"


@pytest.mark.asyncio
async def test_a_follow_up_is_not_stuck_when_the_worker_dies_after_replying(
    monkeypatch,  # noqa: ANN001
) -> None:
    """TLC NoStuckFollowUp: Submit, Claim, Submit, Complete, WorkerCrash.

    The worker commits its reply, then dies (deploy, OOM, time limit) before the separate
    transaction that promotes the follow-up. The redelivered task finds the turn completed
    and does nothing.
    """
    user_id, thread_id, _ = _seed_runtime_project()
    first = await _send(user_id, thread_id, "make the intro faster")
    follow_up: list[TurnAccepted] = []

    async def _planned(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        follow_up.append(await _send(user_id, thread_id, "and use a calmer song"))
        return _reply("Done, the intro is faster.")

    def _worker_dies(_thread_id):  # noqa: ANN001, ANN202
        raise _WorkerDied("worker process died after the reply committed")

    monkeypatch.setattr(rt, "_plan_with_live_agent", _planned)
    monkeypatch.setattr(rt, "_promote_queued_successor_sync", _worker_dies)
    with pytest.raises(_WorkerDied), patch.object(rt.run_kria_turn, "apply_async"):
        await asyncio.to_thread(rt.run_kria_turn.run, first.turn_id)
    monkeypatch.undo()
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    assert _status(first.turn_id) == "completed"
    assert _status(follow_up[0].turn_id) == "queued"
    redelivered = await asyncio.to_thread(rt.run_kria_turn.run, first.turn_id)
    assert redelivered["status"] == "ignored"

    _sweep()

    assert _status(follow_up[0].turn_id) == "pending"
    later = await _send(user_id, thread_id, "also trim the ending")  # no 409
    assert later.status == "queued"


@pytest.mark.asyncio
async def test_a_follow_up_sent_while_kria_thinks_waits_its_turn(monkeypatch) -> None:  # noqa: ANN001
    """TLC AnswersOnlyUpToItsMessage: Submit, Claim, Submit, ReadHistory, Complete.

    Before the fix the follow-up threw the first answer away (revision fence), the first
    turn re-planned with the follow-up in its history, and the follow-up then ran again on
    its own turn: one request acted on twice, and the first answer took two planning runs.
    """
    user_id, thread_id, _ = _seed_runtime_project()
    first = await _send(user_id, thread_id, "make the intro faster")
    follow_up: list[TurnAccepted] = []
    planned_from: list[list[str]] = []

    async def _planned(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        if not follow_up:
            follow_up.append(await _send(user_id, thread_id, "and use a calmer song"))
        planned_from.append(await _history_turn_ids(thread_id))
        return _reply("Done, the intro is faster.")

    monkeypatch.setattr(rt, "_plan_with_live_agent", _planned)
    with patch.object(rt.run_kria_turn, "apply_async") as publish:
        result = await asyncio.to_thread(rt.run_kria_turn.run, first.turn_id)

    assert follow_up[0].status == "queued"
    assert result == {"turn_id": first.turn_id, "status": "completed"}
    assert planned_from == [[first.turn_id]]  # one planning run, without the follow-up
    assert _status(first.turn_id) == "completed"
    assert _status(follow_up[0].turn_id) == "pending"
    assert [c.kwargs["args"][0] for c in publish.call_args_list] == [follow_up[0].turn_id]

    # The follow-up's own turn plans with both messages, and answers it once.
    planned_from.clear()
    result = await asyncio.to_thread(rt.run_kria_turn.run, follow_up[0].turn_id)
    assert result["status"] == "completed"
    assert planned_from == [[first.turn_id, follow_up[0].turn_id]]


@pytest.mark.asyncio
async def test_a_late_promotion_never_starts_a_second_turn(monkeypatch) -> None:  # noqa: ANN001
    """TLC OneAtATime: Submit, Submit, Cancel(follow-up), Submit, CancelPromote.

    Cancelling the waiting follow-up frees the slot; a message sent before the cancel's
    second transaction runs is queued behind the running turn. That promotion then found
    it and started it next to the running turn: two answers planned at once.
    """
    user_id, thread_id, _ = _seed_runtime_project()
    first = await _send(user_id, thread_id, "make the intro faster")
    follow_up = await _send(user_id, thread_id, "and use a calmer song")
    later: list[TurnAccepted] = []
    promote = runtime._promote_queued_successor  # noqa: SLF001

    async def _message_lands_first(db, *, thread_id):  # noqa: ANN001, ANN202
        later.append(await _send(user_id, thread_id, "actually keep the song"))
        return await promote(db, thread_id=thread_id)

    monkeypatch.setattr(runtime, "_promote_queued_successor", _message_lands_first)
    await _cancel(user_id, thread_id, follow_up.turn_id)

    assert later[0].status == "queued"
    assert _status(first.turn_id) == "pending"
    assert _status(later[0].turn_id) == "queued"  # waits for the running turn


@pytest.mark.asyncio
async def test_removing_a_queued_message_answers_instead_of_failing() -> None:
    """The web's edit/remove-queued-message action cancels the queued turn.

    With nothing else queued, the promotion rolled back and expired the loaded rows; the
    response then lazy-loaded them in an async session (MissingGreenlet -> HTTP 500) after
    the cancel had already committed, so the app said it failed and lost the text.
    """
    user_id, thread_id, _ = _seed_runtime_project()
    first = await _send(user_id, thread_id, "make the intro faster")
    queued = await _send(user_id, thread_id, "and use a calmer song")

    engine = create_async_engine(settings.asyncpg_database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            revision = int((await db.get(CreationThread, thread_id)).revision)
            await db.rollback()
            cancelled, successor = await cancel_turn(
                db,
                thread_id=thread_id,
                turn_id=uuid.UUID(queued.turn_id),
                creator_id=user_id,
                expected_thread_revision=revision,
            )
    finally:
        await engine.dispose()

    assert (cancelled.status, successor) == ("cancelled", None)
    assert cancelled.thread_revision == revision + 1
    assert _status(first.turn_id) == "pending"


@pytest.mark.asyncio
async def test_a_late_worker_promotion_never_starts_a_second_turn(monkeypatch) -> None:  # noqa: ANN001
    """TLC OneAtATime, worker path: Complete, ReleaseStranded, Submit, WorkerPromote.

    The worker commits its reply but its promotion runs late: the sweep has already
    started the follow-up, and the creator's next message is queued behind it.
    """
    user_id, thread_id, _ = _seed_runtime_project()
    first = await _send(user_id, thread_id, "make the intro faster")
    follow_up: list[TurnAccepted] = []
    later: list[TurnAccepted] = []
    promote = rt._promote_queued_successor_sync  # noqa: SLF001

    async def _planned(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        follow_up.append(await _send(user_id, thread_id, "and use a calmer song"))
        return _reply("Done, the intro is faster.")

    def _late(thread_id_):  # noqa: ANN001, ANN202
        _sweep()  # starts the follow-up first
        later.append(asyncio.run(_send(user_id, thread_id_, "actually keep the song")))
        return promote(thread_id_)

    monkeypatch.setattr(rt, "_plan_with_live_agent", _planned)
    monkeypatch.setattr(rt, "_promote_queued_successor_sync", _late)
    with patch.object(rt.run_kria_turn, "apply_async"):
        await asyncio.to_thread(rt.run_kria_turn.run, first.turn_id)

    assert _status(follow_up[0].turn_id) == "pending"
    assert later[0].status == "queued"
    assert _status(later[0].turn_id) == "queued"  # waits for the follow-up
