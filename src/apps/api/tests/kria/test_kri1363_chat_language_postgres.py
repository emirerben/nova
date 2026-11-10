"""KRI-1363: through the real `submit_turn`, text for the video never moves the chat language.

The reported runs (see the ticket): an English chat asked for Turkish captions / a Turkish
title and Kria started answering in Turkish. Unit cases: `test_kri1363_chat_language_own_words`.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from app.config import settings
from app.database import engine as async_engine
from app.database import sync_session
from app.models import CreationThread, CreationThreadEvent
from app.services.choice_questions import CONFLICT_TITLE_TEXT

_db_name = make_url(settings.database_url).database or ""
if not _db_name.endswith("_test"):
    pytest.skip(f"refusing to write to non-test database {_db_name!r}", allow_module_level=True)
try:
    with sync_session() as _probe:
        _probe.execute(text("select 1"))
except OperationalError:
    pytest.skip("nova_test Postgres not reachable", allow_module_level=True)

from tests.kria.test_kri520_reply_language_postgres import (  # noqa: E402
    _send,
    _state_and_last_reply,
)
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project  # noqa: E402

TURKISH_CAPTIONS = 'Use these captions: "Bugün Kadıköy\'deyiz. Burası çok güzel. Hadi başlayalım."'
ENGLISH_CAPTIONS = (
    'Altyazıları şöyle yap: "Good morning everyone, today we are in Istanbul and it is great"'
)
BARE_TURKISH_TITLE = "Bugün İstanbul'da çok güzel bir gün geçirdik"


@pytest.fixture(autouse=True)
def _runtime_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)


@pytest.fixture(autouse=True)
async def _fresh_async_pool():  # noqa: ANN202
    yield
    await async_engine.dispose()


def _ask_for_title_words(thread_id: uuid.UUID) -> None:
    """Append Kria's open "what words for the title?" question to the thread."""
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id, with_for_update=True)
        sequence = db.execute(
            select(func.max(CreationThreadEvent.sequence)).where(
                CreationThreadEvent.thread_id == thread_id
            )
        ).scalar_one()
        thread.revision = int(thread.revision) + 1
        db.add(
            CreationThreadEvent(
                thread_id=thread_id,
                sequence=int(sequence) + 1,
                revision=int(thread.revision),
                role="assistant",
                event_type="assistant_response",
                content="What should the title say?",
                payload={
                    "turn_value": "question",
                    "schema_version": 2,
                    "choice_question": {
                        "version": 1,
                        "question_id": str(uuid.uuid4()),
                        "conflict": CONFLICT_TITLE_TEXT,
                        "allow_free_text": True,
                        "options": [{"key": "no_title", "label": "No title"}],
                    },
                },
            )
        )
        db.commit()


@pytest.mark.asyncio
async def test_english_chat_with_turkish_captions_stays_english() -> None:
    user_id, thread_id, _ = _seed_runtime_project()
    await _send(user_id, thread_id, "Status update")
    assert _state_and_last_reply(thread_id)[0]["reply_language"] == "en"

    await _send(user_id, thread_id, TURKISH_CAPTIONS, locale="tr-TR")

    assert _state_and_last_reply(thread_id)[0]["reply_language"] == "en"


@pytest.mark.asyncio
async def test_turkish_chat_with_english_captions_stays_turkish() -> None:
    user_id, thread_id, _ = _seed_runtime_project()
    await _send(user_id, thread_id, "Nasıl gidiyor?", locale="en-US")
    assert _state_and_last_reply(thread_id)[0]["reply_language"] == "tr"

    await _send(user_id, thread_id, ENGLISH_CAPTIONS, locale="en-US")

    assert _state_and_last_reply(thread_id)[0]["reply_language"] == "tr"


@pytest.mark.asyncio
async def test_a_turkish_title_typed_as_the_answer_keeps_the_chat_english() -> None:
    user_id, thread_id, _ = _seed_runtime_project()
    await _send(user_id, thread_id, "Status update")
    _ask_for_title_words(thread_id)

    await _send(user_id, thread_id, BARE_TURKISH_TITLE)

    assert _state_and_last_reply(thread_id)[0]["reply_language"] == "en"


@pytest.mark.asyncio
async def test_the_same_words_with_no_question_open_are_the_creator_switching() -> None:
    user_id, thread_id, _ = _seed_runtime_project()
    await _send(user_id, thread_id, "Status update")

    await _send(user_id, thread_id, BARE_TURKISH_TITLE)

    assert _state_and_last_reply(thread_id)[0]["reply_language"] == "tr"


@pytest.mark.asyncio
async def test_the_kill_switch_is_the_reported_bug(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_reply_language_own_words_enabled", False)
    user_id, thread_id, _ = _seed_runtime_project()
    await _send(user_id, thread_id, "Status update")

    await _send(user_id, thread_id, TURKISH_CAPTIONS)

    assert _state_and_last_reply(thread_id)[0]["reply_language"] == "tr"
