"""Durable acknowledgment for background creator clip preparation."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.routes import creation_threads as routes

ACKNOWLEDGMENT = (
    "I’ve saved your request. I’m analyzing your clips and will continue automatically."
)


def _session(*, creator_id: uuid.UUID, attempt_id: str, status: str = "planning"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=creator_id,
        status=status,
        revision=1,
        active_plan=None,
        preparation={"attempt_id": attempt_id, "status": "queued", "completed": 0, "total": 2},
    )


def _thread(*, creator_id: uuid.UUID, session_id: uuid.UUID):
    return SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=creator_id,
        active_creator_agent_session_id=session_id,
        state={},
    )


def _db(session, events=()):
    return SimpleNamespace(
        get=AsyncMock(return_value=session),
        execute=AsyncMock(
            return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: list(events)))
        ),
    )


@pytest.mark.asyncio
async def test_sync_agent_persists_one_acknowledgment_per_active_preparation_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    creator_id = uuid.uuid4()
    attempt_id = str(uuid.uuid4())
    session = _session(creator_id=creator_id, attempt_id=attempt_id)
    thread = _thread(creator_id=creator_id, session_id=session.id)
    source_event = SimpleNamespace(
        id=uuid.uuid4(), revision=1, event_type="user_message", payload={"message": "Make a reel"}
    )
    thread.state = {
        "creator_agent": routes._creator_agent_projection(session),
        "creator_agent_event_ids": [str(source_event.id)],
    }
    db = _db(session, [source_event])
    receipts: set[str] = set()
    appended: list[dict] = []

    async def duplicate(_db, _thread_id, client_event_id):
        return object() if client_event_id in receipts else None

    async def append(_db, _thread, **kwargs):
        receipts.add(kwargs["client_event_id"])
        appended.append(kwargs)

    monkeypatch.setattr(routes, "_duplicate", duplicate)
    monkeypatch.setattr(routes, "_append", append)

    assert await routes._sync_agent(db, thread) is True
    assert appended == [
        {
            "event_type": "status_update",
            "role": "assistant",
            "content": ACKNOWLEDGMENT,
            "client_event_id": f"creator-preparation:{attempt_id}",
        }
    ]

    assert await routes._sync_agent(db, thread) is False
    assert len(appended) == 1

    next_attempt_id = str(uuid.uuid4())
    session.preparation = {"attempt_id": next_attempt_id, "status": "analyzing"}
    assert await routes._sync_agent(db, thread) is True
    assert appended[-1]["client_event_id"] == f"creator-preparation:{next_attempt_id}"
    assert await routes._sync_agent(db, thread) is False
    assert len(appended) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("session_status", "preparation_status", "attempt_id"),
    [
        ("awaiting_confirmation", "queued", str(uuid.uuid4())),
        ("planning", "ready", str(uuid.uuid4())),
        ("planning", "failed", str(uuid.uuid4())),
        ("planning", "queued", "not-a-uuid"),
        ("revising", "queued", None),
    ],
)
async def test_sync_agent_does_not_ack_terminal_foreign_or_malformed_preparation(
    monkeypatch: pytest.MonkeyPatch,
    session_status: str,
    preparation_status: str,
    attempt_id: str | None,
) -> None:
    creator_id = uuid.uuid4()
    session = _session(
        creator_id=creator_id,
        attempt_id=attempt_id or "",
        status=session_status,
    )
    session.preparation = {"attempt_id": attempt_id, "status": preparation_status}
    thread = _thread(creator_id=creator_id, session_id=session.id)
    append = AsyncMock()
    monkeypatch.setattr(routes, "_append", append)

    await routes._sync_agent(_db(session), thread)

    append.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_agent_does_not_ack_foreign_session(monkeypatch: pytest.MonkeyPatch) -> None:
    owner_id = uuid.uuid4()
    session = _session(creator_id=uuid.uuid4(), attempt_id=str(uuid.uuid4()))
    thread = _thread(creator_id=owner_id, session_id=session.id)
    append = AsyncMock()
    monkeypatch.setattr(routes, "_append", append)

    assert await routes._sync_agent(_db(session), thread) is False
    append.assert_not_awaited()
