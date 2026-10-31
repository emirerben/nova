"""KRI-1066 through the REAL `submit_turn` on Postgres: a typed "skip that" after the ending
picker is stored as that picker's Skip.

`test_kri1066_typed_picker_skip.py` covers the pure matcher and the helper offline. This runs
the transaction itself (thread lock, validation, duplicate-send check, the stored user event):

 P1 a typed "skip that" is stored with the picker's own Skip, the creator's words intact, and
    the idempotency digest still that of the body as sent;
 P2 the same send replayed is the same turn and stores one selection;
 P3 an explicit tap in the same send wins over the typed words;
 P4 an explicit answer to another open question wins and no Skip is derived;
 P5 a derived Skip the validator refuses falls back to a plain message, never a 422;
 P6 the picker flag off, or an ordinary message, derives nothing.

Needs the test database like the other `*_postgres` runtime tests (a database named `*_test`).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria import runtime
from app.kria.api_schemas import SubmitTurnBody
from app.kria.brief import BriefUpdate, load_latest_brief_sync, persist_brief_version_sync
from app.kria.runtime import request_digest, submit_turn
from app.models import CreationThread, CreationThreadEvent
from app.tasks.kria_runtime import _append_sync_event
from tests.kria.test_choice_selection_turns_postgres import CQ
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

PROMPT = "Keep it in chronological order from arrival to the final whistle."
QID = str(uuid.uuid4())
KEY = "order:the-final-whistle"
PICKER = {
    "version": 1,
    "question_id": QID,
    "allow_none": True,
    "categories": [
        {
            "key": KEY,
            "label": "The final whistle",
            "op": "order",
            "intent_id": "order_last_whistle",
            "creator_text": None,
            "candidate_media_ids": ["m1", "m2"],
            "suggested_media_ids": ["m2"],
        }
    ],
}
BRIEF = BriefUpdate(
    kind="order",
    scope="global",
    description="chronological order from arrival to the final whistle",
    facts={"key": "capture_time", "last_clip": "the final whistle"},
)


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "kria_runtime_v2_enabled",
        "main_creator_agent_enabled",
        "kria_clip_selection_questions_enabled",
        "kria_choice_questions_enabled",
        "kria_creative_brief_enabled",
    ):
        monkeypatch.setattr(settings, name, True)


def _body(message: str, thread_id, **fields) -> SubmitTurnBody:  # noqa: ANN001, ANN003
    with sync_session() as db:
        revision = db.get(CreationThread, thread_id).revision
    return SubmitTurnBody(
        message=message,
        client_event_id=fields.pop("client_event_id", f"kri1066-{uuid.uuid4().hex}"),
        expected_thread_revision=revision,
        **fields,
    )


async def _send(user_id, thread_id, body: SubmitTurnBody):  # noqa: ANN001, ANN202
    async with AsyncSessionLocal() as db:
        accepted, _ = await submit_turn(db, thread_id=thread_id, creator_id=user_id, body=body)
    return accepted


async def _thread_after_the_picker(*, with_choice: bool = False):  # noqa: ANN202
    """The prompt as a real turn, the brief it stated, and the picker reply the creator sees."""
    user_id, thread_id, _ = _seed_runtime_project()
    first = await _send(user_id, thread_id, _body(PROMPT, thread_id))
    with sync_session() as db:
        persist_brief_version_sync(
            db, thread_id=thread_id, turn_id=uuid.UUID(first.turn_id), updates=(BRIEF,)
        )
        db.commit()
    with sync_session() as db:
        [req] = load_latest_brief_sync(db, thread_id).live()
        assert req.source_turn_id == first.turn_id  # the ending was stated in the first message
        thread = db.get(CreationThread, thread_id, with_for_update=True)
        _append_sync_event(
            db,
            thread,
            role="assistant",
            event_type="assistant_response",
            content="Tap the clips that show the final whistle.",
            payload={
                "turn_value": "question",
                "clip_question": PICKER,
                **({"choice_question": CQ} if with_choice else {}),
            },
        )
        db.commit()
    return user_id, thread_id


def _stored(thread_id, message: str):  # noqa: ANN001, ANN202
    with sync_session() as db:
        rows = db.execute(
            select(CreationThreadEvent.content, CreationThreadEvent.payload).where(
                CreationThreadEvent.thread_id == thread_id,
                CreationThreadEvent.role == "user",
                CreationThreadEvent.content == message,
            )
        ).all()
    return rows


SKIP = {"question_id": QID, "answers": [], "none_keys": [], "skipped": True}


@pytest.mark.asyncio
async def test_p1_a_typed_skip_is_stored_as_the_pickers_skip_with_the_words_intact() -> None:
    user_id, thread_id = await _thread_after_the_picker()
    try:
        body = _body("skip that", thread_id)
        await _send(user_id, thread_id, body)
        [(content, payload)] = _stored(thread_id, "skip that")
        assert content == "skip that"
        assert payload["clip_selection"] == SKIP
        assert payload["request_digest"] == request_digest(body)  # the body as sent
        assert body.clip_selection is None  # the request itself is never rewritten
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_p2_the_same_send_replayed_is_one_turn_and_one_selection() -> None:
    user_id, thread_id = await _thread_after_the_picker()
    try:
        body = _body("skip that", thread_id, client_event_id="kri1066-replay")
        first = await _send(user_id, thread_id, body)
        replay = await _send(user_id, thread_id, body)
        assert replay.replayed is True and replay.turn_id == first.turn_id
        assert len(_stored(thread_id, "skip that")) == 1
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_p3_an_explicit_tap_wins_over_the_typed_words() -> None:
    user_id, thread_id = await _thread_after_the_picker()
    try:
        tap = {"question_id": QID, "answers": [{"key": KEY, "media_ids": ["m2"]}]}
        await _send(user_id, thread_id, _body("skip that", thread_id, clip_selection=tap))
        [(_, payload)] = _stored(thread_id, "skip that")
        assert payload["clip_selection"]["answers"] == [{"key": KEY, "media_ids": ["m2"]}]
        assert payload["clip_selection"]["skipped"] is False
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_p4_an_explicit_answer_to_another_open_question_wins() -> None:
    user_id, thread_id = await _thread_after_the_picker(with_choice=True)
    try:
        choice = {"question_id": CQ["question_id"], "option_key": "chronological"}
        await _send(user_id, thread_id, _body("skip that", thread_id, choice_selection=choice))
        [(_, payload)] = _stored(thread_id, "skip that")
        assert payload["choice_selection"]["option_key"] == "chronological"
        assert "clip_selection" not in payload  # no Skip was derived beside the explicit answer
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_p5_a_derived_skip_the_validator_refuses_is_a_plain_message_not_a_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, thread_id = await _thread_after_the_picker()
    try:
        real = runtime._validate_clip_selection

        async def stale(db, thread, selection):  # noqa: ANN001, ANN202
            if selection.skipped:  # only the DERIVED Skip is refused
                raise runtime.RuntimeFailure(
                    422, "clip_selection_stale", "stale", recovery="refresh_replan"
                )
            await real(db, thread, selection)

        monkeypatch.setattr(runtime, "_validate_clip_selection", stale)
        accepted = await _send(user_id, thread_id, _body("skip that", thread_id))
        assert accepted.turn_id  # accepted, not a hard error
        [(_, payload)] = _stored(thread_id, "skip that")
        assert "clip_selection" not in payload  # the planner reads the words as any message
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("flag_on, message", [(False, "skip that"), (True, "make it 30 seconds")])
async def test_p6_the_flag_off_or_an_ordinary_message_derives_nothing(
    monkeypatch: pytest.MonkeyPatch, flag_on: bool, message: str
) -> None:
    user_id, thread_id = await _thread_after_the_picker()
    try:
        monkeypatch.setattr(settings, "kria_clip_selection_questions_enabled", flag_on)
        await _send(user_id, thread_id, _body(message, thread_id))
        [(_, payload)] = _stored(thread_id, message)
        assert "clip_selection" not in payload
    finally:
        await async_engine.dispose()
