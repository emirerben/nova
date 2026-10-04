"""KRI-374 lane E: song-order answers over real thread events (validation, 409, storage,
and the assistant question event carrying `song_order_question`)."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria.api_schemas import SubmitTurnBody
from app.kria.contracts import KriaTurnPlan
from app.kria.planner import PlannedKriaTurn
from app.kria.runtime import RuntimeFailure, request_digest, submit_turn
from app.models import CreationThread, CreationThreadEvent
from app.schemas.user_song import SongAlignment, SongOrderAnswerIn, TakeAlignment
from app.services.song_order import build_song_order_question
from app.tasks.kria_runtime import _append_sync_event, run_kria_turn
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

QUESTION = build_song_order_question(
    SongAlignment(
        song_generation=1,
        takes={
            "m1": TakeAlignment(media_id="m1", status="confident", delta_s=4.0, confidence=0.9),
            "m2": TakeAlignment(media_id="m2", status="unmatched"),
        },
    ),
    ["m1", "m2"],
    question_id=str(uuid.uuid4()),
)


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "user_song_montage_enabled", True)


def _ask(thread_id, question=QUESTION) -> None:  # noqa: ANN001
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id, with_for_update=True)
        _append_sync_event(
            db,
            thread,
            role="assistant",
            event_type="assistant_response",
            content="Preview them in this order.",
            payload={
                "turn_value": "question",
                "song_order_question": question.model_dump(mode="json"),
            },
        )
        db.commit()


async def _submit(user_id, thread_id, answer, *, event_id=None, revision=None):  # noqa: ANN001, ANN202
    if revision is None:
        with sync_session() as db:
            revision = db.get(CreationThread, thread_id).revision
    async with AsyncSessionLocal() as db:
        accepted, _ = await submit_turn(
            db,
            thread_id=thread_id,
            creator_id=user_id,
            body=SubmitTurnBody(
                message="Use this order",
                client_event_id=event_id or f"so-{uuid.uuid4().hex}",
                expected_thread_revision=revision,
                song_order=answer,
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
    return [p["song_order"] for p in rows if p and "song_order" in p]


def _answer(ids=("m2", "m1"), qid: str | None = None) -> SongOrderAnswerIn:
    return SongOrderAnswerIn(question_id=qid or QUESTION.question_id, ordered_media_ids=list(ids))


def test_digest_is_unchanged_when_song_order_is_absent() -> None:
    plain = SubmitTurnBody(message="hi", client_event_id="e1", expected_thread_revision=0)
    with_order = plain.model_copy(update={"song_order": _answer()})
    assert request_digest(plain) != request_digest(with_order)
    assert "song_order" not in plain.model_dump(mode="json", exclude_none=True)


@pytest.mark.asyncio
async def test_valid_order_is_stored_and_replays_idempotently() -> None:
    user_id, thread_id, _ = _seed_runtime_project()
    _ask(thread_id)
    try:
        with sync_session() as db:
            revision = db.get(CreationThread, thread_id).revision
        first = await _submit(user_id, thread_id, _answer(), event_id="so-1", revision=revision)
        assert _stored(thread_id) == [_answer().model_dump(mode="json")]
        replay = await _submit(user_id, thread_id, _answer(), event_id="so-1", revision=revision)
        assert replay.replayed is True and replay.turn_id == first.turn_id
        assert len(_stored(thread_id)) == 1
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["unknown_id", "no_open_question", "already_answered"])
async def test_stale_question_is_409_song_order_stale(scenario: str) -> None:
    user_id, thread_id, _ = _seed_runtime_project()
    if scenario != "no_open_question":
        _ask(thread_id)
    try:
        if scenario == "already_answered":
            await _submit(user_id, thread_id, _answer(), event_id="first")
        answer = _answer(qid=str(uuid.uuid4()) if scenario == "unknown_id" else None)
        with pytest.raises(RuntimeFailure) as err:
            await _submit(user_id, thread_id, answer, event_id="second")
        assert err.value.status_code == 409 and err.value.code == "song_order_stale"
        assert err.value.recovery == "refresh_replan"
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("ids", [["m1"], ["m1", "m2", "m3"], ["m1", "m1"], ["m1", "x"]])
async def test_wrong_take_set_or_duplicates_are_422_invalid(ids) -> None:
    user_id, thread_id, _ = _seed_runtime_project()
    _ask(thread_id)
    try:
        with pytest.raises(RuntimeFailure) as err:
            await _submit(user_id, thread_id, _answer(ids))
        assert err.value.status_code == 422 and err.value.code == "song_order_invalid"
        assert _stored(thread_id) == []
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_flag_off_drops_the_answer_and_stores_nothing(monkeypatch) -> None:
    monkeypatch.setattr(settings, "user_song_montage_enabled", False)
    user_id, thread_id, _ = _seed_runtime_project()
    try:
        await _submit(user_id, thread_id, _answer(qid="whatever"))  # not validated either
        assert _stored(thread_id) == []
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("with_question", [True, False])
async def test_question_event_carries_song_order_question_only_when_planned(
    monkeypatch, with_question: bool
) -> None:
    async def _planned(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        return PlannedKriaTurn(
            plan=KriaTurnPlan(
                mode="respond",
                turn_value="question",
                response="Preview them in this order.",
                song_order_question=QUESTION if with_question else None,
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
                    message="Lip sync to my song",
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
            assert payloads[0]["song_order_question"] == QUESTION.model_dump(mode="json")
        else:
            assert "song_order_question" not in payloads[0]
    finally:
        await async_engine.dispose()
