"""KRI-282: server-side validation, storage and replay of clip-picker answers."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria.api_schemas import SubmitTurnBody
from app.kria.runtime import RuntimeFailure, submit_turn
from app.models import CreationThread, CreationThreadEvent
from app.routes.creation_threads import capabilities
from app.tasks.kria_runtime import _append_sync_event
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

QUESTION_ID = str(uuid.uuid4())


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_selection_questions_enabled", True)


def _ask(thread_id, question_id: str = QUESTION_ID) -> None:  # noqa: ANN001
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id, with_for_update=True)
        _append_sync_event(
            db,
            thread,
            role="assistant",
            event_type="assistant_response",
            content="Tap the clips that show dodgeball.",
            payload={
                "turn_value": "question",
                "clip_question": {
                    "version": 1,
                    "question_id": question_id,
                    "allow_none": True,
                    "categories": [
                        {
                            "key": "group:dodgeball",
                            "label": "Dodgeball",
                            "op": "group",
                            "candidate_media_ids": ["m1", "m2", "m3"],
                            "suggested_media_ids": ["m2"],
                        }
                    ],
                },
            },
        )
        db.commit()


async def _submit(
    user_id, thread_id, selection, *, event_id=None, revision=None, message="Dodgeball: clips 1, 2"
):  # noqa: ANN001, ANN202
    if revision is None:
        with sync_session() as db:
            revision = db.get(CreationThread, thread_id).revision
    async with AsyncSessionLocal() as db:
        accepted, _ = await submit_turn(
            db,
            thread_id=thread_id,
            creator_id=user_id,
            body=SubmitTurnBody(
                message=message,
                client_event_id=event_id or f"sel-{uuid.uuid4().hex}",
                expected_thread_revision=revision,
                clip_selection=selection,
            ),
        )
    return accepted


def _stored_selections(thread_id):  # noqa: ANN001, ANN202
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
    return [p["clip_selection"] for p in rows if p and "clip_selection" in p]


def _answer(media_ids=("m1", "m2"), question_id: str = QUESTION_ID) -> dict:  # noqa: ANN001
    return {
        "question_id": question_id,
        "answers": [{"key": "group:dodgeball", "media_ids": list(media_ids)}],
    }


@pytest.mark.asyncio
async def test_valid_selection_is_stored_on_the_user_event_and_replays_idempotently() -> None:
    user_id, thread_id, _ = _seed_runtime_project()
    _ask(thread_id)
    try:
        with sync_session() as db:
            revision = db.get(CreationThread, thread_id).revision
        first = await _submit(user_id, thread_id, _answer(), event_id="sel-1", revision=revision)
        assert _stored_selections(thread_id) == [{**_answer(), "none_keys": [], "skipped": False}]
        replay = await _submit(user_id, thread_id, _answer(), event_id="sel-1", revision=revision)
        assert replay.replayed is True and replay.turn_id == first.turn_id
        assert len(_stored_selections(thread_id)) == 1
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_foreign_media_id_is_rejected() -> None:
    user_id, thread_id, _ = _seed_runtime_project()
    _ask(thread_id)
    try:
        with pytest.raises(RuntimeFailure) as err:
            await _submit(user_id, thread_id, _answer(["m1", "not-offered"]))
        assert err.value.status_code == 422 and err.value.code == "clip_selection_invalid"
        assert _stored_selections(thread_id) == []
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_unknown_category_key_is_rejected() -> None:
    user_id, thread_id, _ = _seed_runtime_project()
    _ask(thread_id)
    try:
        bad = {"question_id": QUESTION_ID, "none_keys": ["group:nope"]}
        with pytest.raises(RuntimeFailure) as err:
            await _submit(user_id, thread_id, bad)
        assert err.value.code == "clip_selection_invalid"
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_stale_or_unknown_question_id_is_rejected() -> None:
    user_id, thread_id, _ = _seed_runtime_project()
    _ask(thread_id)
    try:
        with pytest.raises(RuntimeFailure) as err:
            await _submit(user_id, thread_id, _answer(question_id=str(uuid.uuid4())))
        assert err.value.status_code == 422 and err.value.code == "clip_selection_stale"
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_answering_the_same_question_twice_is_stale() -> None:
    user_id, thread_id, _ = _seed_runtime_project()
    _ask(thread_id)
    try:
        await _submit(user_id, thread_id, _answer(), event_id="a")
        with pytest.raises(RuntimeFailure) as err:
            await _submit(user_id, thread_id, _answer(["m3"]), event_id="b")
        assert err.value.code == "clip_selection_stale"
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_a_selection_with_no_open_question_is_rejected() -> None:
    user_id, thread_id, _ = _seed_runtime_project()
    try:
        with pytest.raises(RuntimeFailure) as err:
            await _submit(user_id, thread_id, _answer())
        assert err.value.code == "clip_selection_stale"
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_flag_off_drops_the_selection_and_stores_nothing(monkeypatch) -> None:
    monkeypatch.setattr(settings, "kria_clip_selection_questions_enabled", False)
    user_id, thread_id, _ = _seed_runtime_project()
    try:
        # Not validated either: an old/rolled-back server just sees a normal message.
        await _submit(user_id, thread_id, _answer(["whatever"]))
        assert _stored_selections(thread_id) == []
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_capability_flag_tracks_the_setting(monkeypatch) -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    assert (await capabilities(user))["clip_selection_questions"] is True
    monkeypatch.setattr(settings, "kria_clip_selection_questions_enabled", False)
    assert (await capabilities(user))["clip_selection_questions"] is False
