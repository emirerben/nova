"""Content-plan endpoints (content-plan Phase 4). All require a real user.

POST /content-plans   — create a plan from the user's ready persona + optional
                        events, enqueue generation.
GET  /content-plans   — the user's latest plan with its items. Each item's live
                        render state is DERIVED from its linked Job.status at read
                        time (plan T2 — no duplicate state machine). Items are
                        eager-loaded with their current_job in 2 queries (T5).
"""

from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth import CurrentUser
from app.database import get_db
from app.models import ContentPlan, PlanItem
from app.models import Persona as PersonaRow
from app.routes.plan_items import PlanItemResponse, plan_item_response

log = structlog.get_logger()
router = APIRouter()

_PERSONA_READY = ("ready", "edited")


class CreatePlanBody(BaseModel):
    # Optional free-text events to bias the plan (trips, launches, exams).
    events: str = ""
    horizon_days: int = 30


class ContentPlanResponse(BaseModel):
    id: str
    plan_status: str
    horizon_days: int
    events: dict | None
    items: list[PlanItemResponse]


def _plan_response(plan: ContentPlan) -> ContentPlanResponse:
    return ContentPlanResponse(
        id=str(plan.id),
        plan_status=plan.plan_status,
        horizon_days=plan.horizon_days,
        events=plan.events,
        items=[plan_item_response(it) for it in plan.items],
    )


@router.post("", response_model=ContentPlanResponse, status_code=status.HTTP_201_CREATED)
async def create_plan(
    body: CreatePlanBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ContentPlanResponse:
    persona = (
        await db.execute(select(PersonaRow).where(PersonaRow.user_id == user.id))
    ).scalar_one_or_none()
    if persona is None or persona.persona_status not in _PERSONA_READY:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Persona must be ready before generating a content plan",
        )

    horizon = max(1, min(body.horizon_days or 30, 60))
    plan = ContentPlan(
        user_id=user.id,
        persona_id=persona.id,
        events={"text": body.events} if body.events else None,
        plan_status="generating",
        horizon_days=horizon,
    )
    db.add(plan)
    await db.commit()
    await db.refresh(plan)

    from app.tasks.content_plan_build import generate_content_plan  # noqa: PLC0415

    generate_content_plan.delay(str(plan.id))
    # Freshly created — no items yet (generation runs async).
    return ContentPlanResponse(
        id=str(plan.id),
        plan_status=plan.plan_status,
        horizon_days=plan.horizon_days,
        events=plan.events,
        items=[],
    )


@router.get("", response_model=ContentPlanResponse)
async def get_plan(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ContentPlanResponse:
    # selectinload items + their current_job → 2 queries, not 1 + N (plan T5).
    plan = (
        await db.execute(
            select(ContentPlan)
            .where(ContentPlan.user_id == user.id)
            .order_by(ContentPlan.created_at.desc())
            .options(selectinload(ContentPlan.items).selectinload(PlanItem.current_job))
            .limit(1)
        )
    ).scalar_one_or_none()
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No content plan yet")
    return _plan_response(plan)


class GenerateFirstWeekResponse(BaseModel):
    enqueued: int
    skipped_no_clips: int


@router.post("/{plan_id}/generate-first-week", response_model=GenerateFirstWeekResponse)
async def generate_first_week(
    plan_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> GenerateFirstWeekResponse:
    """Enqueue one render per day-1..7 item that has clips. Empty ones are skipped.

    Each item dispatches to the throttled `plan-jobs` queue (concurrency=1), so
    seven items render one-at-a-time rather than OOM-ing the worker (plan T3).
    """
    try:
        pid = uuid.UUID(plan_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="bad id") from exc
    plan = (
        await db.execute(
            select(ContentPlan)
            .where(ContentPlan.id == pid, ContentPlan.user_id == user.id)
            .options(selectinload(ContentPlan.items))
        )
    ).scalar_one_or_none()
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found")

    from app.tasks.content_plan_build import generate_plan_item_videos  # noqa: PLC0415

    enqueued = 0
    skipped = 0
    for item in plan.items:
        if item.day_index > 7:
            continue
        if item.clip_gcs_paths:
            generate_plan_item_videos.delay(str(item.id))
            enqueued += 1
        else:
            skipped += 1
    return GenerateFirstWeekResponse(enqueued=enqueued, skipped_no_clips=skipped)
