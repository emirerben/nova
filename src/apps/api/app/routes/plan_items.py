"""Plan-item endpoints + shared item serialization (content-plan Phase 4).

PATCH /plan-items/{id} — hand-edit a plan item (theme / idea / filming_suggestion).

Also the home of `derive_item_status` + `plan_item_response`, used here and by
content_plans.py. Live render state is DERIVED from the linked Job.status at read
time (plan T2): `item_status` on the row only ever holds `idea` | `awaiting_clips`;
generating / ready / failed come from the Job so a reaper-killed job can never
leave an item stuck "generating" forever.
"""

from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.music_matcher import _sanitize_text
from app.auth import CurrentUser
from app.database import get_db
from app.models import ContentPlan, PlanItem

log = structlog.get_logger()
router = APIRouter()

# Job.status buckets (mode="content_plan" reuses the generative variant states).
_JOB_READY = {"variants_ready", "variants_ready_partial", "done", "clips_ready"}
_JOB_FAILED = {
    "variants_failed",
    "matching_failed",
    "no_labeled_tracks",
    "processing_failed",
    "posting_failed",
    "cancelled",
}


def derive_item_status(item: PlanItem) -> str:
    """idea | awaiting_clips | generating | ready | failed — derived, never stored."""
    job = item.current_job
    if job is None:
        # No job minted yet: row state is the source of truth (idea/awaiting_clips).
        return item.item_status
    if job.status in _JOB_READY:
        return "ready"
    if job.status in _JOB_FAILED:
        return "failed"
    return "generating"


class PlanItemResponse(BaseModel):
    id: str
    day_index: int
    theme: str
    idea: str
    filming_suggestion: str | None
    clip_gcs_paths: list[str]
    status: str
    current_job_id: str | None
    user_edited: bool


def plan_item_response(item: PlanItem) -> PlanItemResponse:
    return PlanItemResponse(
        id=str(item.id),
        day_index=item.day_index,
        theme=item.theme,
        idea=item.idea,
        filming_suggestion=item.filming_suggestion,
        clip_gcs_paths=list(item.clip_gcs_paths or []),
        status=derive_item_status(item),
        current_job_id=str(item.current_job_id) if item.current_job_id else None,
        user_edited=item.user_edited,
    )


class PlanItemEdit(BaseModel):
    theme: str | None = None
    idea: str | None = None
    filming_suggestion: str | None = None


@router.patch("/{item_id}", response_model=PlanItemResponse)
async def edit_plan_item(
    item_id: str,
    edit: PlanItemEdit,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlanItemResponse:
    try:
        iid = uuid.UUID(item_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="bad id") from exc

    item = await db.get(PlanItem, iid)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan item not found")
    # Ownership: the item's plan must belong to the caller.
    plan = await db.get(ContentPlan, item.content_plan_id)
    if plan is None or plan.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan item not found")

    updates = edit.model_dump(exclude_none=True)
    if "theme" in updates:
        item.theme = _sanitize_text(updates["theme"]) or item.theme
    if "idea" in updates:
        item.idea = _sanitize_text(updates["idea"]) or item.idea
    if "filming_suggestion" in updates:
        item.filming_suggestion = _sanitize_text(updates["filming_suggestion"]) or None
    if updates:
        item.user_edited = True
    await db.commit()
    await db.refresh(item)
    return plan_item_response(item)
