"""Provider-free admission tests for creator-memory learning."""

from __future__ import annotations

import unicodedata
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.creator_memory_learning import (
    classify_memory_candidate,
    eligible_memory_event,
    enqueue_memory_extraction,
    normalize_creator_message,
)


def _event(*, content: str, role: str = "user", event_type: str = "user_message"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        role=role,
        event_type=event_type,
        content=content,
        thread=SimpleNamespace(creator_id=uuid.uuid4()),
    )


def test_normalize_creator_message_removes_controls_and_bounds_text() -> None:
    value = normalize_creator_message("  Always\u0000 use  serif  " + ("x" * 3000))
    assert "\x00" not in value
    assert value.startswith("Always use serif")
    assert len(value) == 2_000


def test_normalize_creator_message_removes_every_unicode_format_control() -> None:
    value = normalize_creator_message("Never\ufeff add\u200b shadows\u2066 to videos")
    assert value == "Never add shadows to videos"
    assert not any(unicodedata.category(char) == "Cf" for char in value)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Always use Playfair Display", "explicit"),
        ("Never add a text shadow", "explicit"),
        ("I prefer calm, understated edits", "soft"),
        ("Use a serif font in this video", "noop"),
        ("status?", "noop"),
    ],
)
def test_classify_memory_candidate(message: str, expected: str) -> None:
    assert classify_memory_candidate(message) == expected


def test_only_user_messages_with_durable_or_soft_language_are_eligible() -> None:
    assert eligible_memory_event(_event(content="Always use captions"))
    assert not eligible_memory_event(_event(content="Always use captions", role="assistant"))
    assert not eligible_memory_event(_event(content="Use this video only"))
    assert not eligible_memory_event(
        _event(content="Always use captions", event_type="status_update")
    )


@pytest.mark.asyncio
async def test_enqueue_is_idempotent_and_keeps_event_transactional() -> None:
    event = _event(content="Never add text shadows")
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db.execute.return_value = result
    db.flush = AsyncMock()
    db.add = MagicMock()

    row = await enqueue_memory_extraction(db, event)

    assert row is not None
    assert row.source_event_id == event.id
    assert row.user_id == event.thread.creator_id
    assert row.status == "pending"
    db.add.assert_called_once_with(row)
    db.flush.assert_awaited_once()
