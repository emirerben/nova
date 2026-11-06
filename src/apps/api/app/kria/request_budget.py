"""How long an approved request is allowed to get, asked by the draft commit and by the serve.

A draft commit pins the creator's whole conversation into the approval binding. It refuses (a
pinned tap answers ``request_too_long``) when the chronological text passes ``MAX_REQUEST_CHARS``
(``creator_request_text_sync``) or when the bound request does (``bound_request``: that text, the
rendered brief and the superseded lines). A card the creator can tap must not be one of those, so
the serve asks the same two questions here, over the same text, instead of keeping a copy of them.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import select

from app.kria.brief import BriefUpdate, CreativeBrief, apply_updates
from app.kria.brief_binding import MAX_REQUEST_CHARS, bound_request
from app.kria.duplicate_send import is_duplicate_answer
from app.kria.removed_messages import is_removed, removed_turn_ids_sync
from app.models import CreationThreadEvent


def creator_request_text_sync(
    db: Any, *, thread_id: uuid.UUID, through_sequence: int | None
) -> str:
    """Every creator instruction up to ``through_sequence`` (the whole thread when None)."""
    query = select(CreationThreadEvent.content, CreationThreadEvent.payload).where(
        CreationThreadEvent.thread_id == thread_id,
        CreationThreadEvent.role == "user",
        CreationThreadEvent.content.is_not(None),
    )
    if through_sequence is not None:
        query = query.where(CreationThreadEvent.sequence <= through_sequence)
    rows = db.execute(query.order_by(CreationThreadEvent.sequence)).all()
    removed = removed_turn_ids_sync(db, thread_id, (payload for _content, payload in rows))
    messages = [
        str(content).strip()
        for content, payload in rows
        # KRI-552: a re-sent copy of a message adds no instruction to the pinned request;
        # Steer now: neither does a waiting message the creator removed.
        if str(content).strip()
        and not is_duplicate_answer(payload)
        and not is_removed(payload, removed)
    ]
    return "\n".join(messages)


def request_over_budget(
    thread_text: str,
    *,
    tap_message: str,
    brief: CreativeBrief | None,
    updates: Iterable[BriefUpdate] = (),
) -> bool:
    """Would the draft commit refuse a tap of ``tap_message`` on this thread, for its length?

    ``thread_text`` is what the thread holds now; the tap's own message joins it, and the commit's
    brief is the one that already holds the requirements the tap writes (``updates``, persisted as
    the next version).
    """
    message = tap_message.strip()
    text = f"{thread_text}\n{message}" if thread_text and message else thread_text or message
    if len(text) > MAX_REQUEST_CHARS:
        return True
    updates = tuple(updates)
    if updates:
        merged = apply_updates(brief, updates, source_turn_id=None)
        brief = merged.model_copy(update={"version": (brief.version if brief else 0) + 1})
    request = bound_request(brief, latest_message=tap_message, full_creator_request=text)
    return len(request) > MAX_REQUEST_CHARS
