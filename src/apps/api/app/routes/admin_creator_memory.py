"""Safe operator controls for creator-memory extraction work."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import CreationThread, CreationThreadEvent, CreatorMemoryOutbox, User
from app.routes.admin import _require_admin
from app.services.creator_memory_learning import CREATOR_MEMORY_PAYLOAD_VERSION

router = APIRouter()


@router.get("/health", dependencies=[Depends(_require_admin)])
async def creator_memory_health(db: AsyncSession = Depends(get_db)) -> dict[str, object]:
    """Return counts and queue age without exposing creator instruction text."""

    rows = (
        await db.execute(
            select(CreatorMemoryOutbox.status, func.count())
            .group_by(CreatorMemoryOutbox.status)
            .order_by(CreatorMemoryOutbox.status)
        )
    ).all()
    payload_versions = (
        await db.execute(
            select(CreatorMemoryOutbox.payload_version, func.count())
            .group_by(CreatorMemoryOutbox.payload_version)
            .order_by(CreatorMemoryOutbox.payload_version)
        )
    ).all()
    result_codes = (
        await db.execute(
            select(CreatorMemoryOutbox.result_code, func.count())
            .where(CreatorMemoryOutbox.result_code.is_not(None))
            .group_by(CreatorMemoryOutbox.result_code)
            .order_by(CreatorMemoryOutbox.result_code)
        )
    ).all()
    oldest = (
        await db.execute(
            select(func.min(CreatorMemoryOutbox.created_at)).where(
                CreatorMemoryOutbox.status.in_(["pending", "leased", "dead"])
            )
        )
    ).scalar_one_or_none()
    age_seconds = None
    if oldest is not None:
        age_seconds = max(0, int((datetime.now(UTC) - oldest).total_seconds()))
    return {
        "counts": {str(status): int(count) for status, count in rows},
        "oldest_pending_age_seconds": age_seconds,
        "payload_versions": {str(version): int(count) for version, count in payload_versions},
        "result_codes": {str(code): int(count) for code, count in result_codes},
    }


@router.post("/outbox/{outbox_id}/retry", dependencies=[Depends(_require_admin)])
async def retry_creator_memory_outbox(
    outbox_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """Make one validated dead-letter row eligible again, idempotently."""

    if not settings.creator_memory_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Creator memory is disabled",
        )

    row = (
        await db.execute(
            select(CreatorMemoryOutbox).where(CreatorMemoryOutbox.id == outbox_id).with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Outbox row not found")
    if row.status == "pending":
        return {"id": str(row.id), "status": row.status, "attempts": int(row.attempts or 0)}
    if row.status != "dead":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only dead-letter rows can be retried",
        )
    owner = await db.get(User, row.user_id)
    if owner is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Owner no longer exists")
    if not bool(getattr(owner, "creator_memory_enabled", True)):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Personalization is paused",
        )
    if int(row.payload_version) != CREATOR_MEMORY_PAYLOAD_VERSION:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Unsupported creator-memory payload version",
        )
    if row.source_event_id is not None:
        owned_source = (
            await db.execute(
                select(CreationThreadEvent.id)
                .join(CreationThread, CreationThread.id == CreationThreadEvent.thread_id)
                .where(
                    CreationThreadEvent.id == row.source_event_id,
                    CreationThread.creator_id == row.user_id,
                )
            )
        ).scalar_one_or_none()
        if owned_source is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Creator-memory source ownership could not be verified",
            )
    row.status = "pending"
    row.available_at = datetime.now(UTC)
    row.lease_until = None
    row.attempts = 0
    if hasattr(row, "result_code"):
        row.result_code = None
    else:
        row.last_error_code = None
    await db.commit()
    return {"id": str(row.id), "status": row.status, "attempts": int(row.attempts or 0)}
