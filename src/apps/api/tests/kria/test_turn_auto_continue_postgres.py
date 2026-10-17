"""KRI-709: a turn that stops on a time budget is re-run by the server, not by "go ahead".

Prod (2026-10-16, Aston Villa - Fenerbahce): a long request replied 'Reply "go ahead" in a
moment and I'll pick up where I left off' although nothing was stuck. These tests drive the
real `run_kria_turn` (real `_claim`, `_continue_turn`, `_complete_response_turn`,
reconciler) against Postgres; only the planner is stubbed, to return the pending plan.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.kria.api_schemas import SubmitTurnBody
from app.kria.brief import BriefUpdate
from app.kria.contracts import KriaTurnPlan
from app.kria.planner import (
    PlannedKriaTurn,
    _clip_intent_pending_reply,
    continuation_superseded_reply,
)
from app.kria.runtime import submit_turn
from app.models import CreationThreadEvent, CreativeBriefVersion, CreatorAgentTurn
from app.tasks.kria_runtime import reconcile_kria_turns, run_kria_turn
from tests.kria.test_runtime_postgres_integration import (
    _SWEPT_FIRST,
    _record_publishes,
    _seed_runtime_project,
)

_db_name = make_url(settings.database_url).database or ""
if not _db_name.endswith("_test"):
    pytest.skip(f"refusing to write to non-test database {_db_name!r}", allow_module_level=True)
try:
    with sync_session() as _probe:
        _probe.execute(text("select 1"))
except OperationalError:
    pytest.skip("nova_test Postgres not reachable", allow_module_level=True)

FINAL_REPLY = "Done: every clip is matched and the draft is ready."


@pytest.fixture(autouse=True)
def _runtime_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)


def _respond(text_: str, *, continuable: bool) -> PlannedKriaTurn:
    return PlannedKriaTurn(
        plan=KriaTurnPlan(mode="respond", turn_value="recovery", response=text_),
        manifest_hash="a" * 64,
        context_hash="b" * 64,
        continuable=continuable,
    )


def _script_planner(monkeypatch: pytest.MonkeyPatch, *results: PlannedKriaTurn) -> list[int]:
    """The planner returns `results` in order (the last repeats); returns its call log."""
    calls: list[int] = []

    async def _planned(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        calls.append(len(calls))
        return results[min(len(calls) - 1, len(results) - 1)]

    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", _planned)
    return calls


async def _submit(user_id, thread_id, message, revision):  # noqa: ANN001, ANN202
    async with AsyncSessionLocal() as db:
        accepted, _ = await submit_turn(
            db,
            thread_id=thread_id,
            creator_id=user_id,
            body=SubmitTurnBody(
                message=message,
                client_event_id=f"continue-{uuid.uuid4().hex}",
                expected_thread_revision=revision,
            ),
        )
    return accepted


def _assistant_replies(thread_id: uuid.UUID) -> list[str]:
    with sync_session() as db:
        return [
            row.content
            for row in db.execute(
                select(CreationThreadEvent)
                .where(
                    CreationThreadEvent.thread_id == thread_id,
                    CreationThreadEvent.event_type == "assistant_response",
                )
                .order_by(CreationThreadEvent.sequence)
            ).scalars()
        ]


def _sweep_first(turn_id: uuid.UUID) -> None:
    """The reconciler sweeps the 50 oldest recoverable turns; rows other tests left in this
    shared database must not push the turn under test out of that window."""
    with sync_session() as db:
        db.execute(
            update(CreatorAgentTurn)
            .where(CreatorAgentTurn.id == turn_id)
            .values(created_at=_SWEPT_FIRST)
        )
        db.commit()


def _publishes(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Full `apply_async` kwargs of every `run_kria_turn` publish."""
    published: list[dict] = []
    monkeypatch.setattr(run_kria_turn, "apply_async", lambda **kw: published.append(kw))
    return published


@pytest.mark.asyncio
async def test_pending_turn_runs_again_and_the_creator_only_sees_the_final_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, thread_id, _ = _seed_runtime_project()
    pending = _respond(_clip_intent_pending_reply(), continuable=True)
    calls = _script_planner(monkeypatch, pending, _respond(FINAL_REPLY, continuable=False))
    published = _publishes(monkeypatch)
    accepted = await _submit(user_id, thread_id, "Caption every clip with what happens", 2)
    turn_id = uuid.UUID(accepted.turn_id)

    first = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)

    assert first == {"turn_id": accepted.turn_id, "status": "continued"}
    assert _assistant_replies(thread_id) == []  # nothing for the chat to settle on yet
    assert len(published) == 1
    assert published[0]["args"] == [accepted.turn_id]
    assert published[0]["countdown"] == 5
    assert published[0]["task_id"] == f"{accepted.turn_id}-c1"
    with sync_session() as db:
        turn = db.get(CreatorAgentTurn, turn_id)
        assert turn.status == "pending"
        assert turn.lease_owner is None
        assert turn.abandoned_claims == 0  # a clean release is not an abandoned claim
        # Stamped ahead so the 60 s sweep leaves it for the countdown.
        assert turn.lease_expires_at > db.execute(select(func.now())).scalar_one()

    # The sweep must not republish the turn while it waits for its countdown. Seeded first in
    # the sweep's window (50 oldest rows) so the absence below is the lease stamp's doing.
    _sweep_first(turn_id)
    swept = _record_publishes(monkeypatch)
    reconcile_kria_turns.run()
    assert [accepted.turn_id] not in swept

    second = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)

    assert second == {"turn_id": accepted.turn_id, "status": "completed"}
    assert len(calls) == 2
    assert _assistant_replies(thread_id) == [FINAL_REPLY]


@pytest.mark.asyncio
async def test_a_stuck_turn_stops_at_the_run_cap_with_the_old_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The AI really is stuck once the cap is spent: today's reply, not a retry loop, and
    at most `max_runs` planner calls (each can pay for a Main Creator call)."""
    user_id, thread_id, _ = _seed_runtime_project()
    pending_reply = _clip_intent_pending_reply()
    calls = _script_planner(monkeypatch, _respond(pending_reply, continuable=True))
    published = _publishes(monkeypatch)
    accepted = await _submit(user_id, thread_id, "Caption every clip with what happens", 2)
    max_runs = settings.kria_turn_auto_continue_max_runs

    for _ in range(max_runs - 1):
        result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
        assert result["status"] == "continued"
    assert len(published) == max_runs - 1 and _assistant_replies(thread_id) == []

    result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)

    assert result == {"turn_id": accepted.turn_id, "status": "completed"}
    assert len(published) == max_runs - 1  # no further publish
    assert len(calls) == max_runs
    assert _assistant_replies(thread_id) == [pending_reply]


@pytest.mark.asyncio
async def test_a_turn_that_waited_queued_is_still_continued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A follow-up queued behind a long turn is old before it plans: the cap counts runs."""
    user_id, thread_id, _ = _seed_runtime_project()
    _script_planner(monkeypatch, _respond(_clip_intent_pending_reply(), continuable=True))
    published = _publishes(monkeypatch)
    accepted = await _submit(user_id, thread_id, "Caption every clip with what happens", 2)
    with sync_session() as db:
        db.execute(
            update(CreatorAgentTurn)
            .where(CreatorAgentTurn.id == uuid.UUID(accepted.turn_id))
            .values(created_at=func.now() - timedelta(hours=1))
        )
        db.commit()

    result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)

    assert result["status"] == "continued"
    assert len(published) == 1


@pytest.mark.asyncio
async def test_the_brief_is_persisted_once_across_continue_and_final(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A continued run writes no brief version; the final run writes exactly one."""
    user_id, thread_id, _ = _seed_runtime_project()
    updates = (BriefUpdate(kind="text", scope="title", literal="20K Kosu"),)

    def _with_brief(planned: PlannedKriaTurn) -> PlannedKriaTurn:
        return replace(planned, brief_updates=updates, brief_expected_version=0)

    _script_planner(
        monkeypatch,
        _with_brief(_respond(_clip_intent_pending_reply(), continuable=True)),
        _with_brief(_respond(FINAL_REPLY, continuable=False)),
    )
    _publishes(monkeypatch)
    accepted = await _submit(user_id, thread_id, "Title it 20K Kosu and caption every clip", 2)

    def _versions() -> int:
        with sync_session() as db:
            return db.scalar(
                select(func.count())
                .select_from(CreativeBriefVersion)
                .where(CreativeBriefVersion.thread_id == thread_id)
            )

    assert (await asyncio.to_thread(run_kria_turn.run, accepted.turn_id))["status"] == "continued"
    assert _versions() == 0
    assert (await asyncio.to_thread(run_kria_turn.run, accepted.turn_id))["status"] == "completed"
    assert _versions() == 1


@pytest.mark.asyncio
async def test_kill_switch_keeps_the_old_reply_byte_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, thread_id, _ = _seed_runtime_project()
    monkeypatch.setattr(settings, "kria_turn_auto_continue_max_runs", 1)
    pending_reply = _clip_intent_pending_reply()
    _script_planner(monkeypatch, _respond(pending_reply, continuable=True))
    published = _publishes(monkeypatch)
    accepted = await _submit(user_id, thread_id, "Caption every clip with what happens", 2)

    result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)

    assert result == {"turn_id": accepted.turn_id, "status": "completed"}
    assert published == []
    assert _assistant_replies(thread_id) == [pending_reply]


@pytest.mark.asyncio
async def test_a_reply_that_needs_the_creator_never_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`continuable` is the planner's call: a question or a provider failure is not pending."""
    user_id, thread_id, _ = _seed_runtime_project()
    _script_planner(
        monkeypatch, _respond("Which clips should I use for that part?", continuable=False)
    )
    published = _publishes(monkeypatch)
    accepted = await _submit(user_id, thread_id, "Use the best clips", 2)

    result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)

    assert result == {"turn_id": accepted.turn_id, "status": "completed"}
    assert published == []
    assert _assistant_replies(thread_id) == ["Which clips should I use for that part?"]


@pytest.mark.asyncio
async def test_a_newer_message_closes_the_pending_turn_and_runs_next(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The KRI-552 class: the creator typed again while the clips were being checked."""
    user_id, thread_id, _ = _seed_runtime_project()
    _script_planner(monkeypatch, _respond(_clip_intent_pending_reply(), continuable=True))
    published = _publishes(monkeypatch)
    first = await _submit(user_id, thread_id, "Caption every clip with what happens", 2)
    second = await _submit(user_id, thread_id, "Actually make it shorter", first.thread_revision)
    assert second.status == "queued"

    result = await asyncio.to_thread(run_kria_turn.run, first.turn_id)

    assert result == {"turn_id": first.turn_id, "status": "completed"}
    assert _assistant_replies(thread_id) == [continuation_superseded_reply()]
    assert "go ahead" not in continuation_superseded_reply().lower()
    assert [kw["args"] for kw in published] == [[second.turn_id]]  # the newer message runs
    with sync_session() as db:
        assert db.get(CreatorAgentTurn, uuid.UUID(second.turn_id)).status == "pending"


@pytest.mark.asyncio
async def test_a_turn_cancelled_while_planning_stays_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, thread_id, _ = _seed_runtime_project()
    published = _publishes(monkeypatch)
    accepted = await _submit(user_id, thread_id, "Caption every clip with what happens", 2)
    turn_id = uuid.UUID(accepted.turn_id)

    async def _planned(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        with sync_session() as db:  # the creator cancels while the planner is working
            db.execute(
                update(CreatorAgentTurn)
                .where(CreatorAgentTurn.id == turn_id)
                .values(status="cancelled", lease_owner=None, lease_expires_at=None)
            )
            db.commit()
        return _respond(_clip_intent_pending_reply(), continuable=True)

    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", _planned)

    result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)

    assert result == {"turn_id": accepted.turn_id, "status": "ignored"}
    assert published == [] and _assistant_replies(thread_id) == []
    with sync_session() as db:
        assert db.get(CreatorAgentTurn, turn_id).status == "cancelled"


@pytest.mark.asyncio
async def test_a_lost_publish_is_recovered_by_the_sweep_once_the_stamp_lapses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, thread_id, _ = _seed_runtime_project()
    _script_planner(monkeypatch, _respond(_clip_intent_pending_reply(), continuable=True))

    def _broker_down(**_kw):  # noqa: ANN003, ANN202
        raise ConnectionError("broker down")

    monkeypatch.setattr(run_kria_turn, "apply_async", _broker_down)
    accepted = await _submit(user_id, thread_id, "Caption every clip with what happens", 2)
    turn_id = uuid.UUID(accepted.turn_id)

    result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)

    assert result["status"] == "continued"  # a publish failure never fails the turn
    with sync_session() as db:
        db.execute(
            update(CreatorAgentTurn)
            .where(CreatorAgentTurn.id == turn_id)
            .values(lease_expires_at=func.now() - timedelta(seconds=1))
        )
        db.commit()
    _sweep_first(turn_id)
    swept = _record_publishes(monkeypatch)
    reconcile_kria_turns.run()
    assert [str(turn_id)] in swept
