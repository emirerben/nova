"""Per-user "my" surface — the video library + one-off → plan attach (Phase 1 spine).

GET  /me/jobs                       — the signed-in user's videos (the library)
POST /me/jobs/{job_id}/add-to-plan  — pin a standalone video onto a plan day

STRICT auth only: every endpoint uses `CurrentUser` (never `CurrentUserOrSynthetic`),
so the user scope comes from the validated `X-User-Id` header — never a client param.
There is no `user_id` query input to forge, so the list is IDOR-safe by construction;
cross-user references on add-to-plan return 404 (not 403) so we don't leak which ids exist.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentUser
from app.database import get_db
from app.models import ContentPlan, Job, PlanItem

log = structlog.get_logger()
router = APIRouter()

# Job.status buckets — kept in lockstep with plan_items.derive_item_status so the
# library tiles and the plan dashboard agree on "ready"/"failed" across every job mode
# (generative variants, content_plan, template, music, auto_music).
_JOB_READY = {
    "variants_ready",
    "variants_ready_partial",
    "done",
    "clips_ready",
    "template_ready",
    "music_ready",
}
_JOB_FAILED = {
    "variants_failed",
    "matching_failed",
    "no_labeled_tracks",
    "processing_failed",
    "posting_failed",
    "cancelled",
}

_DEFAULT_LIMIT = 24
_MAX_LIMIT = 60


def _derived_status(job: Job) -> str:
    """ready | generating | failed — derived from Job.status, never stored."""
    if job.status in _JOB_READY:
        return "ready"
    if job.status in _JOB_FAILED:
        return "failed"
    return "generating"


def _preview_url(job: Job) -> str | None:
    """One playable URL for the library tile, across every job mode.

    Generative/content_plan jobs keep per-variant outputs in
    `assembly_plan["variants"][*]["output_url"]` (only "ready" variants have one);
    template/music jobs store a single `assembly_plan["output_url"]`.
    """
    plan = job.assembly_plan or {}
    variants = plan.get("variants")
    if isinstance(variants, list):
        for v in variants:
            if v.get("render_status") == "ready" and v.get("output_url"):
                return v["output_url"]
        return None
    url = plan.get("output_url")
    return url if isinstance(url, str) else None


def _job_mode(job: Job) -> str:
    # `mode` is the Phase-3 discriminator; fall back to the legacy job_type.
    return job.mode or job.job_type or "default"


class LibraryJob(BaseModel):
    id: str
    mode: str  # generative | content_plan | template | music | auto_music | default
    status: str  # derived: ready | generating | failed
    raw_status: str
    output_url: str | None
    created_at: datetime
    content_plan_item_id: str | None


def _to_library_job(job: Job, *, content_plan_item_id: str | None = None) -> LibraryJob:
    return LibraryJob(
        id=str(job.id),
        mode=_job_mode(job),
        status=_derived_status(job),
        raw_status=job.status,
        output_url=_preview_url(job),
        created_at=job.created_at,
        content_plan_item_id=(
            content_plan_item_id
            if content_plan_item_id is not None
            else (str(job.content_plan_item_id) if job.content_plan_item_id else None)
        ),
    )


class LibraryResponse(BaseModel):
    jobs: list[LibraryJob]
    next_cursor: str | None


@router.get("/jobs", response_model=LibraryResponse)
async def list_my_jobs(
    user: CurrentUser,
    limit: int = Query(_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    cursor: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
) -> LibraryResponse:
    """The signed-in user's videos, newest first. Strictly scoped to `user.id`.

    Keyset-paginated on `created_at` (indexed): pass the prior page's `next_cursor`
    back as `cursor` to fetch older rows.
    """
    q = select(Job).where(Job.user_id == user.id)
    if cursor:
        try:
            before = datetime.fromisoformat(cursor)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="bad cursor"
            ) from exc
        q = q.where(Job.created_at < before)
    q = q.order_by(Job.created_at.desc()).limit(limit + 1)

    rows = list((await db.execute(q)).scalars().all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = rows[-1].created_at.isoformat() if has_more and rows else None
    return LibraryResponse(jobs=[_to_library_job(j) for j in rows], next_cursor=next_cursor)


class AddToPlanBody(BaseModel):
    day_index: int


@router.post("/jobs/{job_id}/add-to-plan", response_model=LibraryJob)
async def add_job_to_plan(
    job_id: str,
    body: AddToPlanBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> LibraryJob:
    """Pin a standalone video onto a day in the caller's content plan.

    Verifies BOTH the job and the target plan day belong to the caller, then links
    them via the existing circular FK pair (`plan_items.current_job_id` +
    `jobs.content_plan_item_id`). No migration — both columns already exist.
    """
    try:
        jid = uuid.UUID(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="bad id") from exc

    job = (await db.execute(select(Job).where(Job.id == jid))).scalar_one_or_none()
    if job is None or job.user_id != user.id:
        # 404 (not 403) so a caller can't probe which job ids exist.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    plan = (
        await db.execute(
            select(ContentPlan)
            .where(ContentPlan.user_id == user.id)
            .order_by(ContentPlan.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if plan is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No content plan to add to"
        )

    item = (
        await db.execute(
            select(PlanItem).where(
                PlanItem.content_plan_id == plan.id,
                PlanItem.day_index == body.day_index,
            )
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan day not found")

    item.current_job_id = job.id
    job.content_plan_item_id = item.id
    await db.commit()
    log.info("add_job_to_plan", job_id=job_id, day_index=body.day_index, user_id=str(user.id))
    return _to_library_job(job, content_plan_item_id=str(item.id))
