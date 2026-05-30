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

from app import storage
from app.auth import CurrentUser
from app.database import get_db
from app.models import ContentPlan, PlanItem
from app.models import Persona as PersonaRow
from app.routes.plan_items import (
    _ALLOWED_CONTENT_TYPES,
    _MAX_BYTES_PER_FILE,
    _MAX_CLIPS_PER_ITEM,
    PlanItemResponse,
    UploadUrlItem,
    UploadUrlsBody,
    UploadUrlsResponse,
    derive_item_status,
    plan_item_response,
)

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
    # Activation seed (T8): poll scalar + how many seed clips are uploaded. The
    # full seed path list is intentionally not on the wire (count is enough for UI).
    activation_status: str = "none"
    seed_clip_count: int = 0


def _plan_response(plan: ContentPlan) -> ContentPlanResponse:
    return ContentPlanResponse(
        id=str(plan.id),
        plan_status=plan.plan_status,
        horizon_days=plan.horizon_days,
        events=plan.events,
        items=[plan_item_response(it) for it in plan.items],
        activation_status=plan.activation_status,
        seed_clip_count=len(plan.seed_clip_paths or []),
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


@router.post("/{plan_id}/regenerate", response_model=ContentPlanResponse)
async def regenerate_plan(
    plan_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ContentPlanResponse:
    """Re-tune the plan from the user's feedback (feedback loop, Phase 2).

    User-triggered "regenerate plan with my feedback". Rolls the user's video
    feedback into a bounded preference_summary and regenerates — but PROTECTED days
    (hand-edited or already rendering) are kept verbatim by the task (the "their
    say" rule). 409 if a (re)generation is already in flight.
    """
    plan = await _load_owned_plan(plan_id, user.id, db, with_items=True)
    if plan.plan_status == "generating":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A plan generation is already in progress",
        )
    plan.plan_status = "generating"
    await db.commit()

    from app.tasks.content_plan_build import regenerate_content_plan  # noqa: PLC0415

    regenerate_content_plan.delay(str(plan.id))
    return _plan_response(await _load_owned_plan(plan_id, user.id, db, with_items=True))


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


# ── Activation seed (T8): upload recent clips → auto-match → instant first video ──


async def _load_owned_plan(
    plan_id: str, user_id: uuid.UUID, db: AsyncSession, *, with_items: bool = False
) -> ContentPlan:
    try:
        pid = uuid.UUID(plan_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="bad id") from exc
    stmt = select(ContentPlan).where(ContentPlan.id == pid, ContentPlan.user_id == user_id)
    if with_items:
        stmt = stmt.options(selectinload(ContentPlan.items).selectinload(PlanItem.current_job))
    plan = (await db.execute(stmt)).scalar_one_or_none()
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found")
    return plan


@router.post("/{plan_id}/seed-upload-urls", response_model=UploadUrlsResponse)
async def create_seed_upload_urls(
    plan_id: str,
    body: UploadUrlsBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> UploadUrlsResponse:
    """Signed PUT URLs for the activation-seed batch, under the persistent
    users/{uid}/plan/{plan_id}/seed/ prefix (NOT swept by the 24h GCS rule)."""
    plan = await _load_owned_plan(plan_id, user.id, db)
    if not body.files or len(body.files) > _MAX_CLIPS_PER_ITEM:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Provide 1-{_MAX_CLIPS_PER_ITEM} files",
        )
    urls: list[UploadUrlItem] = []
    for f in body.files:
        if f.content_type not in _ALLOWED_CONTENT_TYPES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unsupported content type: {f.content_type}",
            )
        if f.file_size_bytes <= 0 or f.file_size_bytes > _MAX_BYTES_PER_FILE:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Bad file size")
        safe_name = f"{uuid.uuid4().hex}-{f.filename.split('/')[-1]}"
        url, gcs_path = storage.presigned_put_url_for_plan_seed(
            user_id=str(user.id),
            plan_id=str(plan.id),
            filename=safe_name,
            content_type=f.content_type,
        )
        urls.append(UploadUrlItem(upload_url=url, gcs_path=gcs_path))
    return UploadUrlsResponse(urls=urls)


class AttachSeedClipsBody(BaseModel):
    clip_gcs_paths: list[str]


@router.post("/{plan_id}/seed-clips", response_model=ContentPlanResponse)
async def attach_seed_clips(
    plan_id: str,
    body: AttachSeedClipsBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ContentPlanResponse:
    """Record the uploaded seed batch on the plan (validated to the seed prefix)."""
    plan = await _load_owned_plan(plan_id, user.id, db, with_items=True)
    expected = f"users/{user.id}/plan/{plan.id}/seed/"
    for p in body.clip_gcs_paths:
        if not p.startswith(expected):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Clip path outside this plan's seed upload prefix",
            )
    plan.seed_clip_paths = list(body.clip_gcs_paths)
    plan.activation_status = "seeding"
    await db.commit()
    return _plan_response(await _load_owned_plan(plan_id, user.id, db, with_items=True))


@router.post("/{plan_id}/activate", response_model=ContentPlanResponse)
async def activate_plan(
    plan_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ContentPlanResponse:
    """Kick off clip→item matching + auto-generation for the uploaded seed batch."""
    plan = await _load_owned_plan(plan_id, user.id, db, with_items=True)
    if plan.plan_status not in _PERSONA_READY:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Plan must be ready before activation",
        )
    if not (plan.seed_clip_paths or []):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Upload at least one seed clip before activating",
        )
    if plan.activation_status == "activating":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Activation already in progress",
        )
    plan.activation_status = "activating"
    await db.commit()

    from app.tasks.content_plan_build import activate_content_plan  # noqa: PLC0415

    activate_content_plan.delay(str(plan.id))
    return _plan_response(await _load_owned_plan(plan_id, user.id, db, with_items=True))


class ActivationStatusResponse(BaseModel):
    activation_status: str
    seed_clip_count: int
    generating_item_ids: list[str]
    ready_item_ids: list[str]


@router.get("/{plan_id}/activation", response_model=ActivationStatusResponse)
async def get_activation(
    plan_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ActivationStatusResponse:
    """Poll target for the activation seed. Item lists are DERIVED from Job.status."""
    plan = await _load_owned_plan(plan_id, user.id, db, with_items=True)
    generating: list[str] = []
    ready: list[str] = []
    for it in plan.items:
        st = derive_item_status(it)
        if st == "generating":
            generating.append(str(it.id))
        elif st == "ready":
            ready.append(str(it.id))
    return ActivationStatusResponse(
        activation_status=plan.activation_status,
        seed_clip_count=len(plan.seed_clip_paths or []),
        generating_item_ids=generating,
        ready_item_ids=ready,
    )
