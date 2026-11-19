"""Green 5 (2026-11-04): a chat that starts with a stock chip answers in the creator's language.

The creator (who types English: Green 2, 3 and 4 were all English) opened a new chat on a phone
set to Turkish and tapped "Suggest an edit." first. The chat had no language yet, so it fell back
to ``Accept-Language`` (tr) and the whole chip turn came back in Turkish.

Since the model-owned reply language (2026-11-16), no message is re-read or scored: a new chat
with no stored language starts from the STORED language of the creator's latest other chat
(what that chat's model decided), then the device language. The turn's model then reports the
language of this chat's own message.

Ways it could fail:
1. A chip-first chat still takes the device language although the creator's latest other chat
   has a stored language.
2. Another chat's messages are re-scored instead of its stored language read (a Turkish lyrics
   paste in an English chat would then decide the new chat).
3. A creator with no stored language anywhere loses the device fallback (KRI-784 behaviour).
4. Another creator's chats leak into this creator's language.
5. The switch off is the device language again.

Evidence level: real `submit_turn` and the real create route on the local test Postgres, no
model call.
"""

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
from app.models import (
    ContentPlan,
    CreationThread,
    CreationThreadEvent,
    CreatorAgentSession,
    Persona,
    PlanItem,
    User,
)

_db_name = make_url(settings.database_url).database or ""
if not _db_name.endswith("_test"):
    pytest.skip(f"refusing to write to non-test database {_db_name!r}", allow_module_level=True)
try:
    with sync_session() as _probe:
        _probe.execute(text("select 1"))
except OperationalError:
    pytest.skip("nova_test Postgres not reachable", allow_module_level=True)

from tests.kria.test_runtime_postgres_integration import _seed_runtime_project  # noqa: E402

GREEN4_MESSAGE = (
    "Yorumla episode 9: Kervan x Lumo Pamukkale show. Make the whole video like a green screen: "
    "me small at the bottom right in front, and behind me full screen whichever photo or video "
    "I'm talking about, changing every 3-5 seconds. Cut the pauses and the sentence I restarted. "
    "Captions small, white, lowercase, in the middle. No music."
)
CHIP = "Suggest an edit."
TURKISH_PHONE = "tr-TR,tr;q=0.9"


@pytest.fixture(autouse=True)
def _runtime_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)


@pytest.fixture(autouse=True)
async def _fresh_async_pool():  # noqa: ANN202
    yield
    await async_engine.dispose()


def _second_thread(user_id: uuid.UUID) -> uuid.UUID:
    """A new chat for the same creator: its own plan, item and session under their persona,
    built the way `_seed_runtime_project` builds the first one."""
    plan_id, item_id, session_id, thread_id = (uuid.uuid4() for _ in range(4))
    with sync_session() as db:
        persona = db.execute(select(Persona).where(Persona.user_id == user_id)).scalar_one()
        db.add(ContentPlan(id=plan_id, user_id=user_id, persona_id=persona.id))
        db.flush()
        db.add(
            PlanItem(
                id=item_id,
                content_plan_id=plan_id,
                position=1,
                idea="Yorumla episode 9",
                item_status="awaiting_clips",
            )
        )
        db.flush()
        db.add(CreatorAgentSession(id=session_id, creator_id=user_id, plan_item_id=item_id))
        db.flush()
        db.add(
            CreationThread(
                id=thread_id,
                creator_id=user_id,
                runtime_version=2,
                content_plan_id=plan_id,
                active_plan_item_id=item_id,
                active_creator_agent_session_id=session_id,
                title="Green 5",
                revision=2,
                state={
                    "media": [{"media_id": "clip-1", "filename": "IMG_7231.mov"}],
                    "media_count": 1,
                    "edit_format": "talking_to_camera",
                },
            )
        )
        db.flush()
        db.add_all(
            [
                CreationThreadEvent(
                    thread_id=thread_id,
                    sequence=0,
                    revision=1,
                    role="system",
                    event_type="thread_created",
                    content=None,
                    payload=None,
                ),
                CreationThreadEvent(
                    thread_id=thread_id,
                    sequence=1,
                    revision=2,
                    role="assistant",
                    event_type="format_prompt",
                    content="What are we making?",
                    payload={"kind": "select_format"},
                ),
            ]
        )
        db.commit()
    return thread_id


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
                client_event_id=f"green45-{uuid.uuid4().hex}",
                expected_thread_revision=revision,
            ),
            locale=locale,
        )
    return accepted


def _language(thread_id: uuid.UUID) -> str | None:
    with sync_session() as db:
        return (db.get(CreationThread, thread_id).state or {}).get("reply_language")


def _store(thread_id: uuid.UUID, language: str) -> None:
    """What the earlier chat's model decided, stored with its turn."""
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        thread.state = {**(thread.state or {}), "reply_language": language}
        db.commit()


async def _english_creator_with_new_chat() -> tuple[uuid.UUID, uuid.UUID]:
    user_id, earlier, _ = _seed_runtime_project()
    # The earlier chat holds Turkish lyrics the creator pasted, but its model decided English.
    await _send(user_id, earlier, GREEN4_MESSAGE, locale=TURKISH_PHONE)
    await _send(user_id, earlier, "Vazgeçmem dünyaları\nÖnüme serseler de", locale=TURKISH_PHONE)
    _store(earlier, "en")
    return user_id, _second_thread(user_id)


@pytest.mark.asyncio
async def test_green5_chip_first_takes_the_latest_chats_stored_language() -> None:
    """1, 2: the stored English of the earlier chat wins over its Turkish lyrics and the phone."""
    user_id, green5 = await _english_creator_with_new_chat()

    await _send(user_id, green5, CHIP, locale=TURKISH_PHONE)

    assert _language(green5) == "en"


@pytest.mark.asyncio
async def test_a_typed_message_is_not_scored_at_submit() -> None:
    """The new chat starts from the stored language; this turn's model decides the rest."""
    user_id, chat = await _english_creator_with_new_chat()

    await _send(user_id, chat, "Videoyu daha kısa yapar mısın lütfen?", locale="en-US")

    assert _language(chat) == "en"


@pytest.mark.asyncio
async def test_a_creator_with_no_stored_language_keeps_the_device_fallback() -> None:
    """3: nothing stored anywhere, the device decides."""
    user_id, earlier, _ = _seed_runtime_project()
    await _send(user_id, earlier, CHIP)  # no locale: nothing stored
    assert _language(earlier) is None
    chat = _second_thread(user_id)

    await _send(user_id, chat, CHIP, locale=TURKISH_PHONE)

    assert _language(chat) == "tr"


@pytest.mark.asyncio
async def test_another_creators_chats_do_not_count() -> None:
    """4: one creator's English chats never decide another creator's language."""
    await _english_creator_with_new_chat()
    other, chat, _ = _seed_runtime_project()

    await _send(other, chat, CHIP, locale=TURKISH_PHONE)

    assert _language(chat) == "tr"


@pytest.mark.asyncio
async def test_switch_off_is_the_device_language(monkeypatch: pytest.MonkeyPatch) -> None:
    """5: off, the chip-first chat takes the device language."""
    monkeypatch.setattr(settings, "kria_reply_language_history_enabled", False)
    user_id, green5 = await _english_creator_with_new_chat()

    await _send(user_id, green5, CHIP, locale=TURKISH_PHONE)

    assert _language(green5) == "tr"


def _greeting(user_id: uuid.UUID, *, accept_language: str) -> str:
    """POST /creation-threads as ``user_id`` (the real route) and return the format prompt."""
    from fastapi.testclient import TestClient

    from app.auth import get_current_user
    from app.main import app

    with sync_session() as db:
        user = db.get(User, user_id)
        db.expunge(user)
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        response = TestClient(app).post(
            "/creation-threads", json={}, headers={"Accept-Language": accept_language}
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert response.status_code in (200, 201), response.text
    thread_id = uuid.UUID(response.json()["id"])
    with sync_session() as db:
        return db.execute(
            select(CreationThreadEvent.content).where(
                CreationThreadEvent.thread_id == thread_id,
                CreationThreadEvent.event_type == "format_prompt",
            )
        ).scalar_one()


@pytest.mark.asyncio
async def test_a_new_chat_greets_in_the_latest_chats_stored_language() -> None:
    user_id, _green5 = await _english_creator_with_new_chat()
    await async_engine.dispose()  # the route runs on the test client's own event loop

    assert _greeting(user_id, accept_language=TURKISH_PHONE).startswith("What are we making?")


@pytest.mark.asyncio
async def test_a_new_creator_is_greeted_in_the_device_language() -> None:
    """3: nothing stored, the device language greets as before (KRI-784)."""
    user_id, _thread, _ = _seed_runtime_project()

    assert _greeting(user_id, accept_language=TURKISH_PHONE).startswith("Ne yapıyoruz?")
