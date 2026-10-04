"""KRI-282: validation, storage and replay of conflict-choice answers + question event."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria.api_schemas import SubmitTurnBody
from app.kria.contracts import KriaTurnPlan
from app.kria.planner import PlannedKriaTurn
from app.kria.runtime import RuntimeFailure, submit_turn
from app.models import CreationThread, CreationThreadEvent
from app.routes.creation_threads import capabilities
from app.tasks.kria_runtime import _append_sync_event, run_kria_turn
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

QUESTION_ID = str(uuid.uuid4())
CQ = {
    "version": 1,
    "question_id": QUESTION_ID,
    "conflict": "order_vs_group",
    "options": [
        {"key": "group_first", "label": "Group by sport", "recommended": True},
        {"key": "chronological", "label": "Keep it chronological", "recommended": False},
    ],
    "allow_free_text": True,
}


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "kria_choice_questions_enabled", True)


def _ask(thread_id, question=CQ) -> None:  # noqa: ANN001
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id, with_for_update=True)
        _append_sync_event(
            db,
            thread,
            role="assistant",
            event_type="assistant_response",
            content="Which do you prefer?",
            payload={"turn_value": "question", "choice_question": question},
        )
        db.commit()


async def _submit(user_id, thread_id, selection, *, event_id=None, revision=None):  # noqa: ANN001, ANN202
    if revision is None:
        with sync_session() as db:
            revision = db.get(CreationThread, thread_id).revision
    async with AsyncSessionLocal() as db:
        accepted, _ = await submit_turn(
            db,
            thread_id=thread_id,
            creator_id=user_id,
            body=SubmitTurnBody(
                message="Group by sport first",
                client_event_id=event_id or f"ch-{uuid.uuid4().hex}",
                expected_thread_revision=revision,
                choice_selection=selection,
            ),
        )
    return accepted


def _stored(thread_id):  # noqa: ANN001, ANN202
    with sync_session() as db:
        rows = (
            db.execute(
                select(CreationThreadEvent.payload).where(
                    CreationThreadEvent.thread_id == thread_id, CreationThreadEvent.role == "user"
                )
            )
            .scalars()
            .all()
        )
    return [p["choice_selection"] for p in rows if p and "choice_selection" in p]


def _answer(option="group_first", question_id: str = QUESTION_ID) -> dict:
    return {"question_id": question_id, "option_key": option}


@pytest.mark.asyncio
async def test_valid_choice_is_stored_and_replays_idempotently() -> None:
    user_id, thread_id, _ = _seed_runtime_project()
    _ask(thread_id)
    try:
        with sync_session() as db:
            revision = db.get(CreationThread, thread_id).revision
        first = await _submit(user_id, thread_id, _answer(), event_id="ch-1", revision=revision)
        assert _stored(thread_id) == [_answer()]
        replay = await _submit(user_id, thread_id, _answer(), event_id="ch-1", revision=revision)
        assert replay.replayed is True and replay.turn_id == first.turn_id
        assert len(_stored(thread_id)) == 1
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_unoffered_option_is_invalid() -> None:
    user_id, thread_id, _ = _seed_runtime_project()
    _ask(thread_id)
    try:
        with pytest.raises(RuntimeFailure) as err:
            await _submit(user_id, thread_id, _answer("shuffle"))
        assert err.value.status_code == 422 and err.value.code == "choice_selection_invalid"
        assert _stored(thread_id) == []
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_unknown_question_or_no_open_question_is_stale() -> None:
    user_id, thread_id, _ = _seed_runtime_project()
    try:
        with pytest.raises(RuntimeFailure) as err:
            await _submit(user_id, thread_id, _answer())  # nothing asked yet
        assert err.value.status_code == 422 and err.value.code == "choice_selection_stale"
        _ask(thread_id)
        with pytest.raises(RuntimeFailure) as err:
            await _submit(user_id, thread_id, _answer(question_id=str(uuid.uuid4())))
        assert err.value.code == "choice_selection_stale"
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_answering_twice_is_stale() -> None:
    user_id, thread_id, _ = _seed_runtime_project()
    _ask(thread_id)
    try:
        await _submit(user_id, thread_id, _answer(), event_id="a")
        with pytest.raises(RuntimeFailure) as err:
            await _submit(user_id, thread_id, _answer("chronological"), event_id="b")
        assert err.value.code == "choice_selection_stale"
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_flag_off_drops_the_choice_and_stores_nothing(monkeypatch) -> None:
    monkeypatch.setattr(settings, "kria_choice_questions_enabled", False)
    user_id, thread_id, _ = _seed_runtime_project()
    try:
        await _submit(user_id, thread_id, _answer("whatever"))
        assert _stored(thread_id) == []
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_capability_flag_tracks_the_setting(monkeypatch) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    assert (await capabilities(user))["choice_questions"] is True
    monkeypatch.setattr(settings, "kria_choice_questions_enabled", False)
    assert (await capabilities(user))["choice_questions"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("with_question", [True, False])
async def test_question_event_carries_choice_question_only_when_planned(
    monkeypatch, with_question: bool
) -> None:
    async def _planned(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        return PlannedKriaTurn(
            plan=KriaTurnPlan(
                mode="respond",
                turn_value="question",
                response="Which do you prefer?",
                choice_question=CQ if with_question else None,
            ),
            manifest_hash="a" * 64,
            context_hash="b" * 64,
        )

    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", _planned)
    user_id, thread_id, _ = _seed_runtime_project()
    try:
        with sync_session() as db:
            revision = db.get(CreationThread, thread_id).revision
        async with AsyncSessionLocal() as db:
            accepted, _ = await submit_turn(
                db,
                thread_id=thread_id,
                creator_id=user_id,
                body=SubmitTurnBody(
                    message="Chronological, grouped by sport",
                    client_event_id=f"q-{uuid.uuid4().hex}",
                    expected_thread_revision=revision,
                ),
            )
        await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
        with sync_session() as db:
            payloads = [
                p
                for p in db.execute(
                    select(CreationThreadEvent.payload).where(
                        CreationThreadEvent.thread_id == thread_id,
                        CreationThreadEvent.event_type == "assistant_response",
                    )
                )
                .scalars()
                .all()
                if p and p.get("turn_value") == "question"
            ]
        assert len(payloads) == 1
        if with_question:
            assert payloads[0]["choice_question"] == CQ
        else:
            assert "choice_question" not in payloads[0]
    finally:
        await async_engine.dispose()
