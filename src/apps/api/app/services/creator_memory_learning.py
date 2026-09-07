"""Safe, deterministic admission checks for creator-memory extraction.

This module deliberately does not decide or persist a memory rule. The direction
service owns that mutation. It only decides whether a committed creator message
is worth sending to the bounded extractor and provides a provider-free fallback
classification for tests and local development.
"""

from __future__ import annotations

from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import CreationThread, CreationThreadEvent, CreatorMemoryOutbox, User
from app.services.private_text import normalize_private_text

MemoryCandidate = Literal["explicit", "soft", "noop"]

CREATOR_MEMORY_PAYLOAD_VERSION = 1
CREATOR_MEMORY_EXTRACTOR_VERSION = "creator-memory-v1"
_MAX_SOURCE_CHARS = 2_000
_DURABLE_MARKERS = (
    "always",
    "never",
    "from now on",
    "every video",
    "all future videos",
    "going forward",
    "for future videos",
    "her zaman",
    "asla",
    "bundan sonra",
    "tüm videolarımda",
    "siempre",
    "nunca",
    "a partir de ahora",
    "toujours",
    "jamais",
    "désormais",
)
_LOCAL_MARKERS = (
    "this video",
    "this project",
    "this edit",
    "this time",
    "just this once",
    "bu video",
    "bu proje",
    "sadece bu sefer",
    "este video",
    "ce projet",
)
_SOFT_MARKERS = (
    "i prefer",
    "i like",
    "i usually",
    "my style",
    "my videos are",
    "please keep",
    "tercih ederim",
    "genellikle",
    "videolarım",
    "prefiero",
    "je préfère",
)
_STATEFUL_MARKERS = (
    "forget",
    "stop using",
    "no longer",
    "instead",
    "replace",
    "change my",
    "switch from",
    "unut",
    "artık kullanma",
    "deja de usar",
    "oublie",
)


def normalize_creator_message(message: str | None) -> str:
    """Normalize bounded creator text before classification or provider use."""

    if not isinstance(message, str):
        return ""
    normalized = normalize_private_text(message)
    return " ".join(normalized.split())[:_MAX_SOURCE_CHARS].strip()


def classify_memory_candidate(message: str | None) -> MemoryCandidate:
    """Classify durable language without making an executable memory decision.

    Explicit durable language is eligible for activation by the direction
    extractor. Softer language is eligible only for an inert suggestion. Local
    language and ordinary conversation are safe no-ops.
    """

    text = normalize_creator_message(message)
    if not text:
        return "noop"
    lowered = text.casefold()
    if any(marker in lowered for marker in _LOCAL_MARKERS):
        return "noop"
    if any(marker in lowered for marker in _DURABLE_MARKERS):
        return "explicit"
    if any(marker in lowered for marker in _SOFT_MARKERS):
        return "soft"
    return "noop"


def requires_stateful_extraction(message: str | None) -> bool:
    """Return whether explicit language needs current-memory judgment."""

    lowered = normalize_creator_message(message).casefold()
    return any(marker in lowered for marker in _STATEFUL_MARKERS)


def eligible_memory_event(event: CreationThreadEvent) -> bool:
    """Return whether an event may create one extraction outbox row."""

    return (
        event.role == "user"
        and event.event_type == "user_message"
        and bool(normalize_creator_message(event.content))
        and classify_memory_candidate(event.content) != "noop"
    )


async def enqueue_memory_extraction(
    db: AsyncSession,
    event: CreationThreadEvent,
) -> CreatorMemoryOutbox | None:
    """Enqueue one idempotent extraction row in the event transaction."""

    if not eligible_memory_event(event):
        return None
    existing = (
        await db.execute(
            select(CreatorMemoryOutbox).where(
                CreatorMemoryOutbox.source_event_id == event.id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    outbox = CreatorMemoryOutbox(
        user_id=event.thread.creator_id,
        source_event_id=event.id,
        status="pending",
        extractor_version=CREATOR_MEMORY_EXTRACTOR_VERSION,
    )
    # The source payload is kept on the outbox in the final schema so project
    # deletion cannot erase leased work. Use conditional assignment while the
    # additive migration rolls through mixed workers.
    if hasattr(outbox, "source_message"):
        outbox.source_message = normalize_creator_message(event.content)
    if hasattr(outbox, "payload_version"):
        outbox.payload_version = CREATOR_MEMORY_PAYLOAD_VERSION
    db.add(outbox)
    await db.flush()
    if classify_memory_candidate(event.content) == "explicit":
        await apply_explicit_memory_event(db, event=event, outbox=outbox)
    return outbox


async def apply_explicit_memory_event(
    db: AsyncSession,
    *,
    event: CreationThreadEvent,
    outbox: CreatorMemoryOutbox,
):
    """Apply durable language before its project can cross a generation boundary.

    Soft preference inference remains asynchronous. Explicit ``always``/``never``
    language uses the same deterministic, provider-free service path as the
    extraction worker so the first render cannot race its account snapshot.
    """

    if not settings.creator_memory_enabled or outbox.status != "pending":
        return None
    owner = await db.get(User, outbox.user_id)
    if owner is None:
        outbox.status = "succeeded"
        outbox.result_code = "deleted"
        outbox.lease_until = None
        return None
    if not bool(getattr(owner, "creator_memory_enabled", True)):
        outbox.status = "succeeded"
        outbox.result_code = "paused"
        outbox.lease_until = None
        return None
    if requires_stateful_extraction(event.content):
        return None

    from app.services.creator_direction import (  # noqa: PLC0415
        CreatorDirectionService,
        LimitReached,
    )

    try:
        operation = await CreatorDirectionService().create_item(
            db,
            outbox.user_id,
            instruction=normalize_creator_message(event.content),
            category="other",
            enforcement="constraint",
            normalized_key=None,
            structured_value=None,
            expected_revision=int(getattr(owner, "creator_memory_revision", 0)),
            idempotency_key=f"creator-memory:{outbox.id}",
            source_kind="creation_thread",
            source_thread_id=event.thread_id,
            source_event_id=event.id,
            user_locked=False,
            initial_state="active",
        )
    except LimitReached:
        outbox.status = "succeeded"
        outbox.result_code = "limit_reached"
        return None

    outbox.status = "succeeded"
    outbox.result_code = operation.operation_kind
    outbox.lease_until = None
    await append_memory_receipt(
        db,
        user_id=outbox.user_id,
        thread_id=event.thread_id,
        operation=operation,
        candidate="explicit",
    )
    return operation


async def append_memory_receipt(
    db: AsyncSession,
    *,
    user_id,
    thread_id,
    operation,
    candidate: str,
) -> None:
    """Append one visible, idempotent receipt for an automatic memory change."""

    if (
        thread_id is None
        or candidate != "explicit"
        or getattr(operation, "actor_kind", None) != "system"
        or getattr(operation, "operation_kind", None)
        not in {"create_item", "extracted_supersede", "extracted_forget"}
        or getattr(operation, "undo_expires_at", None) is None
    ):
        return

    thread = (
        await db.execute(
            select(CreationThread)
            .where(CreationThread.id == thread_id, CreationThread.creator_id == user_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if thread is None:
        return

    operation_id = str(operation.id)
    client_event_id = f"creator-memory-receipt:{operation_id}"
    existing = (
        await db.execute(
            select(CreationThreadEvent.id).where(
                CreationThreadEvent.thread_id == thread.id,
                CreationThreadEvent.client_event_id == client_event_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return

    sequence = (
        int(
            (
                await db.execute(
                    select(func.coalesce(func.max(CreationThreadEvent.sequence), -1)).where(
                        CreationThreadEvent.thread_id == thread.id
                    )
                )
            ).scalar_one()
        )
        + 1
    )
    thread.revision = int(thread.revision) + 1
    copy = {
        "create_item": "Remembered for future videos.",
        "extracted_supersede": "Updated what I remember for future videos.",
        "extracted_forget": "Stopped using that preference for future videos.",
    }[operation.operation_kind]
    db.add(
        CreationThreadEvent(
            thread_id=thread.id,
            sequence=sequence,
            client_event_id=client_event_id,
            role="assistant",
            event_type="memory_updated",
            content=copy,
            payload={
                "kind": "creator_memory_receipt",
                "operation_id": operation_id,
                "memory_revision": int(operation.resulting_revision),
                "revision": int(operation.resulting_revision),
                "undo_expires_at": operation.undo_expires_at.isoformat(),
            },
            revision=thread.revision,
        )
    )
    await db.flush()


__all__ = [
    "MemoryCandidate",
    "CREATOR_MEMORY_EXTRACTOR_VERSION",
    "CREATOR_MEMORY_PAYLOAD_VERSION",
    "classify_memory_candidate",
    "append_memory_receipt",
    "apply_explicit_memory_event",
    "eligible_memory_event",
    "enqueue_memory_extraction",
    "normalize_creator_message",
    "requires_stateful_extraction",
]
