"""Safe, deterministic admission checks for creator-memory extraction.

This module deliberately does not decide or persist a memory rule. The direction
service owns that mutation. It only decides whether a committed creator message
is worth sending to the bounded extractor and provides a provider-free fallback
classification for tests and local development.
"""

from __future__ import annotations

from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CreationThreadEvent, CreatorMemoryOutbox
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
    return outbox


__all__ = [
    "MemoryCandidate",
    "CREATOR_MEMORY_EXTRACTOR_VERSION",
    "CREATOR_MEMORY_PAYLOAD_VERSION",
    "classify_memory_candidate",
    "eligible_memory_event",
    "enqueue_memory_extraction",
    "normalize_creator_message",
]
