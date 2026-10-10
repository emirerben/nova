"""Read-only, owner-scoped thought-summary projections (KRI-557)."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentUser
from app.config import settings
from app.database import get_db
from app.models import ContentPlan, CreationThread, PlanItem, ThoughtSummary

router = APIRouter()


class ThoughtSummaryOut(BaseModel):
    id: uuid.UUID
    client_request_id: str
    status: str
    text: str
    started_at: datetime
    completed_at: datetime | None = None
    duration_ms: int | None = None


class ThoughtSummaryListOut(BaseModel):
    client_request_id: str | None = None
    summaries: list[ThoughtSummaryOut]


def _out(row: ThoughtSummary) -> ThoughtSummaryOut:
    return ThoughtSummaryOut(
        id=row.id,
        client_request_id=row.client_request_id,
        status=row.status,
        text=row.text,
        started_at=row.started_at,
        completed_at=row.completed_at,
        duration_ms=(
            int((row.completed_at - row.started_at).total_seconds() * 1000)
            if row.completed_at is not None and row.started_at is not None
            else None
        ),
    )


async def _owned_thread(
    thread_id: uuid.UUID, user: CurrentUser, db: AsyncSession
) -> CreationThread:
    thread = await db.scalar(
        select(CreationThread).where(
            CreationThread.id == thread_id, CreationThread.creator_id == user.id
        )
    )
    if thread is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Creation thread not found."
        )
    return thread


@router.get("/creation-threads/{thread_id}/thought-summaries", response_model=ThoughtSummaryListOut)
async def list_creation_thought_summaries(
    thread_id: uuid.UUID,
    user: CurrentUser,
    client_request_id: str | None = Query(default=None, min_length=1, max_length=160),
    db: AsyncSession = Depends(get_db),
) -> ThoughtSummaryListOut:
    await _owned_thread(thread_id, user, db)
    if not settings.thought_summaries_enabled:
        return ThoughtSummaryListOut(client_request_id=client_request_id, summaries=[])
    stmt = select(ThoughtSummary).where(
        ThoughtSummary.thread_id == thread_id,
        ThoughtSummary.creator_id == user.id,
        ThoughtSummary.status != "failed",
    )
    if client_request_id is not None:
        stmt = stmt.where(ThoughtSummary.client_request_id == client_request_id)
    else:
        # Reopened history intentionally exposes only successful summaries.
        stmt = stmt.where(ThoughtSummary.status == "completed")
    rows = (await db.scalars(stmt.order_by(ThoughtSummary.started_at))).all()
    return ThoughtSummaryListOut(
        client_request_id=client_request_id,
        summaries=[_out(row) for row in rows],
    )


@router.get(
    "/plan-items/{item_id}/slide-post/thought-summaries",
    response_model=ThoughtSummaryListOut,
)
async def list_slide_post_thought_summaries(
    item_id: uuid.UUID,
    user: CurrentUser,
    client_request_id: str | None = Query(default=None, min_length=1, max_length=160),
    db: AsyncSession = Depends(get_db),
) -> ThoughtSummaryListOut:
    # Match the slide-post route's PlanItem -> ContentPlan ownership fence. A
    # proposal is valid before a creation thread exists.
    owned = await db.scalar(
        select(PlanItem.id)
        .join(ContentPlan, ContentPlan.id == PlanItem.content_plan_id)
        .where(PlanItem.id == item_id, ContentPlan.user_id == user.id)
    )
    if owned is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Slide post not found.")
    if not settings.thought_summaries_enabled:
        return ThoughtSummaryListOut(client_request_id=client_request_id, summaries=[])
    stmt = select(ThoughtSummary).where(
        ThoughtSummary.plan_item_id == item_id,
        ThoughtSummary.creator_id == user.id,
        ThoughtSummary.status != "failed",
    )
    if client_request_id is not None:
        stmt = stmt.where(ThoughtSummary.client_request_id == client_request_id)
    else:
        stmt = stmt.where(ThoughtSummary.status == "completed")
    rows = (await db.scalars(stmt.order_by(ThoughtSummary.started_at))).all()
    return ThoughtSummaryListOut(
        client_request_id=client_request_id,
        summaries=[_out(row) for row in rows],
    )
