"""Authenticated current native activity. No media URLs or action authority."""

import uuid
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentUser
from app.database import get_db
from app.models import NativeActivity
from app.services.native_activity import activity_query, is_current

router = APIRouter()


class ActivityOut(BaseModel):
    id: uuid.UUID
    thread_id: uuid.UUID
    kind: Literal["video_ready", "action_required"]
    created_at: datetime
    read_at: datetime | None


class ActivityPage(BaseModel):
    items: list[ActivityOut]
    next_before: str | None = None


def public_activity(activity):
    return ActivityOut(
        id=activity.id,
        thread_id=activity.thread_id,
        kind=activity.kind,
        created_at=activity.created_at,
        read_at=activity.read_at,
    )


@router.get("/activities", response_model=ActivityPage)
async def list_activities(
    user: CurrentUser,
    before: uuid.UUID | None = None,
    limit: int = Query(30, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
):
    query = activity_query(user.id).order_by(
        NativeActivity.created_at.desc(), NativeActivity.id.desc()
    )
    if before is not None:
        cursor = (await db.execute(query.where(NativeActivity.id == before))).first()
        if cursor is None:
            raise HTTPException(404, "Activity not found")
        date = cursor[0].created_at
        query = query.where(
            (NativeActivity.created_at < date)
            | ((NativeActivity.created_at == date) & (NativeActivity.id < before))
        )
    # Scan bounded pages so stale entries cannot hide a current item behind them.
    items = []
    offset = 0
    last = None
    while len(items) < limit and offset < 1000:
        rows = (await db.execute(query.offset(offset).limit(100))).all()
        if not rows:
            last = None
            break
        for activity, job in rows:
            last = activity
            if is_current(activity, job):
                items.append(public_activity(activity))
                if len(items) == limit:
                    break
        offset += len(rows)
        if len(rows) < 100 and len(items) < limit:
            last = None
            break
    return ActivityPage(items=items, next_before=str(last.id) if last else None)


async def current_activity(db, owner_id, activity_id):
    row = (
        await db.execute(activity_query(owner_id).where(NativeActivity.id == activity_id))
    ).first()
    if row is None or not is_current(*row):
        raise HTTPException(404, "Activity not found")
    return row[0]


@router.get("/activities/{activity_id}", response_model=ActivityOut)
async def get_activity(
    activity_id: uuid.UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
):
    return public_activity(await current_activity(db, user.id, activity_id))


@router.post("/activities/{activity_id}/read", response_model=ActivityOut)
async def read_activity(
    activity_id: uuid.UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
):
    activity = await current_activity(db, user.id, activity_id)
    if activity.read_at is None:
        activity.read_at = datetime.now(UTC)
    await db.commit()
    return public_activity(activity)
