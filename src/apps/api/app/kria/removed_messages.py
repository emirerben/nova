"""Steer now: a waiting message the creator took back (trash or Edit) is gone for Kria too.

Removing a waiting message is the plain cancel of its queued turn. With Steer now on, that
cancel marks its ``turn_cancelled`` event ``removed_while_waiting``; every reader that hands
the creator's own words to planning (the pinned full request, the conversation and history
the models read, the unread lines a brief reads, the delegation check) skips the removed
message, the same way it skips a re-sent copy (KRI-552). Nothing is marked with the switch
off, so those readers are unchanged then. docs/pipelines/steer-now.md.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CreationThreadEvent

REMOVED_WHILE_WAITING = "removed_while_waiting"


def _statement(thread_id: uuid.UUID):  # noqa: ANN202
    return select(CreationThreadEvent.payload).where(
        CreationThreadEvent.thread_id == thread_id,
        CreationThreadEvent.event_type == "turn_cancelled",
    )


def _ids(payloads: Any) -> frozenset[str]:
    return frozenset(
        str(payload["turn_id"])
        for payload in payloads
        if isinstance(payload, dict)
        and payload.get(REMOVED_WHILE_WAITING)
        and payload.get("turn_id")
    )


def _any_waited(payloads: Iterable[Any]) -> bool:
    """Only a message sent while Kria worked (queued) can have been removed."""
    return any(isinstance(p, dict) and p.get("turn_status") == "queued" for p in payloads)


async def removed_turn_ids(
    db: AsyncSession, thread_id: uuid.UUID, payloads: Iterable[Any]
) -> frozenset[str]:
    """Turn ids of the waiting messages the creator removed from this chat.

    ``payloads`` are the user messages the caller already read: with none that waited, no
    message can have been removed and no query runs.
    """
    if not _any_waited(payloads):
        return frozenset()
    return _ids((await db.execute(_statement(thread_id))).scalars().all())


def removed_turn_ids_sync(db: Any, thread_id: uuid.UUID, payloads: Iterable[Any]) -> frozenset[str]:
    if not _any_waited(payloads):
        return frozenset()
    return _ids(db.execute(_statement(thread_id)).scalars().all())


def is_removed(payload: Any, removed: frozenset[str]) -> bool:
    """True for the user message of a removed waiting message."""
    return bool(removed) and isinstance(payload, dict) and str(payload.get("turn_id")) in removed
