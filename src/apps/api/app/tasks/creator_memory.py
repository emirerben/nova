"""Reliable creator-memory extraction dispatch.

The dispatcher is intentionally provider-agnostic. It claims durable outbox
rows, then hands each eligible message to the direction service when available.
Missing providers or malformed extraction output are safe no-ops and never block
the creator's project.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.models import (
    CreationThread,
    CreationThreadEvent,
    CreatorMemoryItem,
    CreatorMemoryOutbox,
    User,
)
from app.services.creator_direction_capabilities import (
    SUPPORTED_STRUCTURED_KEYS,
    validate_capability_values,
)
from app.services.creator_memory_learning import (
    CREATOR_MEMORY_PAYLOAD_VERSION,
    classify_memory_candidate,
    normalize_creator_message,
)
from app.worker import celery_app

log = structlog.get_logger()

_CLAIM_BATCH = 50
_LEASE = timedelta(minutes=5)
_MAX_ATTEMPTS = 5
_RETRY_BASE_SECONDS = 30
_RETRY_MAX_SECONDS = 15 * 60


def _retry_delay(*, outbox_id: uuid.UUID, attempts: int) -> timedelta:
    """Return bounded exponential retry delay with deterministic jitter."""

    exponent = max(0, min(int(attempts) - 1, 5))
    base = min(_RETRY_BASE_SECONDS * (2**exponent), _RETRY_MAX_SECONDS)
    digest = hashlib.sha256(f"{outbox_id}:{attempts}".encode()).digest()
    jitter = int.from_bytes(digest[:2], "big") % max(1, base // 4)
    return timedelta(seconds=min(base + jitter, _RETRY_MAX_SECONDS))


def _source_event_is_owned(
    session,
    *,
    source_event_id: uuid.UUID,
    user_id: uuid.UUID,  # noqa: ANN001
) -> bool:
    return (
        session.execute(
            select(CreationThreadEvent.id)
            .join(CreationThread, CreationThread.id == CreationThreadEvent.thread_id)
            .where(
                CreationThreadEvent.id == source_event_id,
                CreationThread.creator_id == user_id,
            )
        ).scalar_one_or_none()
        is not None
    )


def _extract_typed_direction(
    session,
    *,
    user_id: uuid.UUID,
    message: str,
    candidate: str,  # noqa: ANN001
) -> dict | None:
    """Use the model only where current state or soft evidence needs judgment."""

    rows = list(
        session.execute(
            select(CreatorMemoryItem)
            .where(
                CreatorMemoryItem.user_id == user_id,
                CreatorMemoryItem.state.in_(("active", "suggested")),
            )
            .order_by(CreatorMemoryItem.updated_at.desc())
            .limit(20)
        ).scalars()
    )
    # A first unambiguous durable instruction is fully handled by the local
    # typed inference path. Soft evidence and stateful contradictions/revokes
    # require the strict extractor.
    if candidate == "explicit" and not rows:
        return None

    from app.agents._model_client import default_client  # noqa: PLC0415
    from app.agents.creator_memory_extractor import (  # noqa: PLC0415
        CreatorMemoryExtractorAgent,
        CreatorMemoryExtractorInput,
        CurrentMemoryItem,
    )

    current_memory = []
    context_chars = 0
    for row in rows:
        if row.normalized_key is not None and row.normalized_key not in SUPPORTED_STRUCTURED_KEYS:
            continue
        instruction = row.instruction[:500]
        if context_chars + len(instruction) > 4_000:
            break
        current_memory.append(
            CurrentMemoryItem(
                id=str(row.id),
                normalized_key=row.normalized_key,
                instruction=instruction,
                enforcement=row.enforcement,
                state=row.state,
                user_locked=row.user_locked,
            )
        )
        context_chars += len(instruction)
    input_value = CreatorMemoryExtractorInput(
        source_message=message,
        candidate_hint=candidate,
        current_memory=current_memory,
    )
    return CreatorMemoryExtractorAgent(default_client()).run(input_value).model_dump(mode="json")


def _claim_rows(session, *, limit: int = _CLAIM_BATCH) -> list[str]:  # noqa: ANN001
    now = datetime.now(UTC)
    rows = list(
        session.execute(
            select(CreatorMemoryOutbox)
            .where(
                or_(
                    CreatorMemoryOutbox.status == "pending",
                    (CreatorMemoryOutbox.status == "leased")
                    & (CreatorMemoryOutbox.lease_until < now),
                ),
                CreatorMemoryOutbox.available_at <= now,
            )
            .order_by(CreatorMemoryOutbox.created_at)
            .with_for_update(skip_locked=True)
            .limit(max(1, min(limit, 100)))
        )
        .scalars()
        .all()
    )
    task_ids: list[str] = []
    for row in rows:
        row.status = "leased"
        row.attempts = int(row.attempts or 0) + 1
        row.lease_until = now + _LEASE
        task_ids.append(str(row.id))
    session.commit()
    return task_ids


@celery_app.task(name="app.tasks.creator_memory.claim_outbox", soft_time_limit=20, time_limit=30)
def claim_outbox() -> int:
    """Lease due rows and dispatch bounded extraction tasks."""

    if not settings.creator_memory_enabled:
        return 0
    with sync_session() as session:
        ids = _claim_rows(session)
    for outbox_id in ids:
        process_outbox.delay(outbox_id)
    return len(ids)


def _complete(session, row: CreatorMemoryOutbox, *, code: str) -> None:  # noqa: ANN001
    row.status = "succeeded"
    if hasattr(row, "result_code"):
        row.result_code = code
    else:
        row.last_error_code = code
    row.lease_until = None
    session.commit()


async def _apply_deterministic_direction(
    *,
    user_id: uuid.UUID,
    source_event_id: uuid.UUID | None,
    source_thread_id: uuid.UUID | None,
    message: str,
    candidate: str,
    idempotency_key: str,
    session_factory=None,  # noqa: ANN001
    extraction: dict | None = None,
) -> str:
    """Apply the safe no-provider fallback through the canonical service.

    Explicit durable language still works in local development and during
    provider outages. Model-backed soft candidates remain inert suggestions
    until the creator accepts them.
    """

    from app.services.creator_direction import CreatorDirectionService, LimitReached

    factory = session_factory or AsyncSessionLocal
    async with factory() as db:
        # A late task must never recreate state for an erased account.
        owner = await db.get(User, user_id)
        if owner is None:
            return "deleted"
        if not bool(getattr(owner, "creator_memory_enabled", True)):
            return "paused"
        expected_revision = int(getattr(owner, "creator_memory_revision", 0))
        service = CreatorDirectionService()
        extraction = dict(extraction or {})
        if extraction.get("normalized_key") == "font_family" and not validate_capability_values(
            extraction.get("structured_value")
        ):
            # Preserve the creator's durable prose as prompt direction without
            # claiming deterministic renderer enforcement for an unbundled font.
            extraction["normalized_key"] = None
            extraction["structured_value"] = None
        extracted_operation = str((extraction or {}).get("operation") or "")
        if extracted_operation == "noop":
            return "noop"
        try:
            if extracted_operation in {"forget", "supersede"}:
                target = (extraction or {}).get("target_item_id")
                if not target:
                    return "noop"
                operation = await service.apply_extracted_transition(
                    db,
                    user_id,
                    operation_kind=extracted_operation,
                    target_item_id=uuid.UUID(str(target)),
                    instruction=(extraction or {}).get("instruction"),
                    category=(extraction or {}).get("category"),
                    enforcement=(extraction or {}).get("enforcement"),
                    normalized_key=(extraction or {}).get("normalized_key"),
                    structured_value=(extraction or {}).get("structured_value"),
                    expected_revision=expected_revision,
                    idempotency_key=idempotency_key,
                    source_thread_id=source_thread_id,
                    source_event_id=source_event_id,
                )
            else:
                instruction = str((extraction or {}).get("instruction") or message)
                is_suggestion = extracted_operation == "suggest" or (
                    not extracted_operation and candidate == "soft"
                )
                operation = await service.create_item(
                    db,
                    user_id,
                    instruction=instruction,
                    category=str((extraction or {}).get("category") or "other"),
                    enforcement=str(
                        (extraction or {}).get("enforcement")
                        or ("advisory" if is_suggestion else "constraint")
                    ),
                    normalized_key=(extraction or {}).get("normalized_key"),
                    structured_value=(extraction or {}).get("structured_value"),
                    expected_revision=expected_revision,
                    idempotency_key=idempotency_key,
                    source_kind="creation_thread",
                    source_thread_id=source_thread_id,
                    source_event_id=source_event_id,
                    user_locked=False,
                    initial_state="suggested" if is_suggestion else "active",
                )
        except LimitReached:
            await db.rollback()
            return "limit_reached"
        await _append_memory_receipt(
            db,
            user_id=user_id,
            thread_id=source_thread_id,
            operation=operation,
            candidate=candidate,
        )
        await db.commit()
        return operation.operation_kind


async def _append_memory_receipt(
    db,
    *,
    user_id: uuid.UUID,
    thread_id: uuid.UUID | None,
    operation,
    candidate: str,
) -> None:  # noqa: ANN001
    """Append the visible automatic-memory receipt in the same transaction.

    This intentionally does not use the creation-thread ``_append`` helper:
    that helper enqueues every event for extraction, and a system receipt must
    never become another memory candidate. The operation id is also used as a
    deterministic client id so retries/replayed idempotency keys do not add a
    second receipt.
    """

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


async def _apply_on_isolated_engine(**kwargs) -> str:  # noqa: ANN003
    """Run one Celery async write without reusing a connection across loops."""

    engine = create_async_engine(
        settings.asyncpg_database_url,
        poolclass=NullPool,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        return await _apply_deterministic_direction(session_factory=factory, **kwargs)
    finally:
        await engine.dispose()


@celery_app.task(name="app.tasks.creator_memory.process_outbox", soft_time_limit=45, time_limit=60)
def process_outbox(outbox_id: str) -> str:
    """Process one claimed row without allowing provider failure to escape."""

    try:
        parsed_id = uuid.UUID(str(outbox_id))
    except (TypeError, ValueError):
        return "invalid_id"
    with sync_session() as session:
        row = session.execute(
            select(CreatorMemoryOutbox).where(CreatorMemoryOutbox.id == parsed_id).with_for_update()
        ).scalar_one_or_none()
        if row is None or row.status not in {"leased", "pending"}:
            return "ignored"
        if not settings.creator_memory_enabled:
            row.status = "pending"
            row.lease_until = None
            session.commit()
            return "disabled"
        if int(getattr(row, "payload_version", 0) or 0) != CREATOR_MEMORY_PAYLOAD_VERSION:
            row.status = "dead"
            row.result_code = "unsupported_payload_version"
            row.lease_until = None
            session.commit()
            return "unsupported_payload_version"
        owner = session.get(User, row.user_id)
        if owner is None:
            _complete(session, row, code="deleted")
            return "deleted"
        if not bool(getattr(owner, "creator_memory_enabled", True)):
            _complete(session, row, code="paused")
            return "paused"
        event = None
        source_event_id = getattr(row, "source_event_id", None)
        if source_event_id is not None:
            if not _source_event_is_owned(
                session, source_event_id=source_event_id, user_id=row.user_id
            ):
                row.status = "dead"
                row.result_code = "source_owner_mismatch"
                row.lease_until = None
                session.commit()
                return "source_owner_mismatch"
            event = session.execute(
                select(CreationThreadEvent).where(CreationThreadEvent.id == source_event_id)
            ).scalar_one_or_none()
        source_message = normalize_creator_message(
            getattr(row, "source_message", None) or (event.content if event is not None else None)
        )
        if not source_message:
            _complete(session, row, code="source_event_missing")
            return "source_event_missing"
        candidate = classify_memory_candidate(source_message)
        if candidate == "noop":
            _complete(session, row, code="noop")
            return "noop"
        message = source_message
        # The direction service owns writes. Keep this import optional while
        # mixed-version workers roll out; a worker without the service leaves a
        # safe terminal no-op rather than inventing memory state.
        try:
            from app.services.creator_direction import CreatorDirectionService  # noqa: F401
        except ImportError:
            _complete(session, row, code="direction_service_unavailable")
            log.info(
                "creator_memory.extraction_deferred",
                outbox_id=str(row.id),
                candidate=candidate,
            )
            return "direction_service_unavailable"
        try:
            extraction = _extract_typed_direction(
                session,
                user_id=row.user_id,
                message=message,
                candidate=candidate,
            )
            if extraction and extraction.get("operation") == "noop":
                _complete(session, row, code="noop")
                return "noop"
            result = asyncio.run(
                _apply_on_isolated_engine(
                    user_id=row.user_id,
                    source_event_id=source_event_id,
                    source_thread_id=getattr(event, "thread_id", None),
                    message=message,
                    candidate=candidate,
                    idempotency_key=f"creator-memory:{row.id}",
                    extraction=extraction,
                )
            )
        except Exception as exc:  # noqa: BLE001 - extraction is best effort
            attempts = int(row.attempts or 0)
            row.status = "dead" if attempts >= _MAX_ATTEMPTS else "pending"
            error_code = type(exc).__name__[:120]
            if hasattr(row, "result_code"):
                row.result_code = error_code
            else:
                row.last_error_code = error_code
            row.lease_until = None
            if row.status == "pending":
                row.available_at = datetime.now(UTC) + _retry_delay(
                    outbox_id=row.id,
                    attempts=attempts,
                )
            session.commit()
            log.warning(
                "creator_memory.extraction_failed",
                outbox_id=str(row.id),
                error_code=type(exc).__name__,
            )
            return "extraction_failed"
        _complete(session, row, code=str(result or candidate))
        return str(result or candidate)


__all__ = ["claim_outbox", "process_outbox"]
