"""KRI-520: submit_turn stores the chat language on the thread and answers in it."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria.api_schemas import SubmitTurnBody
from app.kria.runtime import submit_turn
from app.models import CreationThread, CreationThreadEvent

_db_name = make_url(settings.database_url).database or ""
if not _db_name.endswith("_test"):
    pytest.skip(f"refusing to write to non-test database {_db_name!r}", allow_module_level=True)
try:
    with sync_session() as _probe:
        _probe.execute(text("select 1"))
except OperationalError:
    pytest.skip("nova_test Postgres not reachable", allow_module_level=True)

from tests.kria.test_runtime_postgres_integration import _seed_runtime_project  # noqa: E402


@pytest.fixture(autouse=True)
def _runtime_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)


@pytest.fixture(autouse=True)
async def _fresh_async_pool():  # noqa: ANN202
    # Each test runs on its own event loop; pooled asyncpg connections must not leak.
    yield
    await async_engine.dispose()


async def _send(user_id, thread_id, message, *, locale=None):  # noqa: ANN001, ANN202
    async with AsyncSessionLocal() as db:
        thread = await db.get(CreationThread, thread_id)
        revision = int(thread.revision)
        await db.rollback()
        accepted, _ = await submit_turn(
            db,
            thread_id=thread_id,
            creator_id=user_id,
            body=SubmitTurnBody(
                message=message,
                client_event_id=f"kri520-{uuid.uuid4().hex}",
                expected_thread_revision=revision,
            ),
            locale=locale,
        )
    return accepted


def _state_and_last_reply(thread_id: uuid.UUID) -> tuple[dict, str | None]:
    with sync_session() as db:
        state = dict(db.get(CreationThread, thread_id).state or {})
        reply = db.execute(
            select(CreationThreadEvent.content)
            .where(
                CreationThreadEvent.thread_id == thread_id,
                CreationThreadEvent.role == "assistant",
            )
            .order_by(CreationThreadEvent.sequence.desc())
            .limit(1)
        ).scalar_one()
    return state, reply


@pytest.mark.asyncio
async def test_turkish_status_question_is_answered_in_turkish_and_remembered() -> None:
    user_id, thread_id, _ = _seed_runtime_project()

    accepted = await _send(user_id, thread_id, "Nasıl gidiyor?", locale="en-US")

    assert accepted.status == "completed"
    state, reply = _state_and_last_reply(thread_id)
    assert state["reply_language"] == "tr"
    assert reply == "Şu anda devam eden bir düzenleme yok. Projenin son hâli kaydedildi."

    # A tap-sent stock sentence and an English device keep the chat Turkish.
    await _send(user_id, thread_id, "Suggest an edit.", locale="en-US")
    state, _ = _state_and_last_reply(thread_id)
    assert state["reply_language"] == "tr"


@pytest.mark.asyncio
async def test_device_language_decides_a_first_message_that_does_not_say() -> None:
    user_id, thread_id, _ = _seed_runtime_project()

    accepted = await _send(user_id, thread_id, "ok", locale="tr-TR,tr;q=0.9")

    assert accepted.status == "pending"
    state, _ = _state_and_last_reply(thread_id)
    assert state["reply_language"] == "tr"


@pytest.mark.asyncio
async def test_english_chat_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    user_id, thread_id, _ = _seed_runtime_project()

    await _send(user_id, thread_id, "Status update")

    state, reply = _state_and_last_reply(thread_id)
    assert state["reply_language"] == "en"
    assert reply == "There’s no edit running right now. Your latest project state is saved."

    monkeypatch.setattr(settings, "kria_reply_language_enabled", False)
    await _send(user_id, thread_id, "Nasıl gidiyor?", locale="tr-TR")
    state, reply = _state_and_last_reply(thread_id)
    assert state["reply_language"] == "en"  # kill switch: nothing new is stored
    assert reply == "There’s no edit running right now. Your latest project state is saved."
