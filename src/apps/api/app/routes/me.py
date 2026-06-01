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
from typing import Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentUser
from app.database import get_db
from app.models import (
    VIDEO_FEEDBACK_THUMB_SIGNALS,
    ContentPlan,
    Job,
    PlanItem,
    VideoFeedback,
)

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
    # The thumb the user left on this video (up | down | more_like_this), or None.
    # Populated batched in list_my_jobs; defaults None elsewhere (the tile keeps its
    # own optimistic state after a write).
    feedback_signal: str | None = None


def _to_library_job(
    job: Job,
    *,
    content_plan_item_id: str | None = None,
    feedback_signal: str | None = None,
) -> LibraryJob:
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
        feedback_signal=feedback_signal,
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

    # Batched thumb lookup for this page — one query, no N+1. One-thumb-per-video is
    # enforced on write, so at most one thumb row per job; newest wins if a race left
    # two. Scoped to user.id (the rows are already the caller's, but defense-in-depth).
    thumbs: dict[uuid.UUID, str] = {}
    if rows:
        fb_rows = (
            await db.execute(
                select(VideoFeedback.job_id, VideoFeedback.signal)
                .where(
                    VideoFeedback.user_id == user.id,
                    VideoFeedback.job_id.in_([j.id for j in rows]),
                    VideoFeedback.signal.in_(VIDEO_FEEDBACK_THUMB_SIGNALS),
                )
                .order_by(VideoFeedback.created_at.desc())
            )
        ).all()
        for job_id, signal in fb_rows:
            thumbs.setdefault(job_id, signal)  # newest first → keep the latest

    return LibraryResponse(
        jobs=[_to_library_job(j, feedback_signal=thumbs.get(j.id)) for j in rows],
        next_cursor=next_cursor,
    )


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


# ── Feedback loop (Phase 2): per-video reactions + plan-level steer notes ─────────


class FeedbackBody(BaseModel):
    # 'up'|'down'|'more_like_this' are mutually-exclusive per video; 'note' carries
    # free text (per-video OR plan-level). Validated as a closed set at the edge.
    signal: Literal["up", "down", "more_like_this", "note"]
    job_id: str | None = None
    content_plan_id: str | None = None
    note: str | None = None


class FeedbackResponse(BaseModel):
    id: str
    signal: str
    job_id: str | None
    content_plan_id: str | None


@router.post("/feedback", response_model=FeedbackResponse, status_code=status.HTTP_201_CREATED)
async def create_feedback(
    body: FeedbackBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> FeedbackResponse:
    """Record a feedback signal on the caller's own video or content plan.

    IDOR-safe: `user_id` is always the authed user (never a body field), and the
    referenced job/plan must belong to the caller (404 otherwise, never 403, so a
    caller can't probe which ids exist). Exactly one of job_id/content_plan_id is
    required. For the three thumb signals we keep at most one per video (delete the
    prior thumb, then insert) so a 👍→👎 flip leaves a single row; `note` rows are
    always additive and can coexist with a thumb.
    """
    if (body.job_id is None) == (body.content_plan_id is None):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Provide exactly one of job_id or content_plan_id",
        )
    if body.signal == "note":
        if not (body.note and body.note.strip()):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="A note signal requires non-empty note text",
            )
    note = body.note.strip() if body.note and body.note.strip() else None

    job_uuid: uuid.UUID | None = None
    plan_uuid: uuid.UUID | None = None
    if body.job_id is not None:
        try:
            job_uuid = uuid.UUID(body.job_id)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="bad id") from exc
        job = (await db.execute(select(Job).where(Job.id == job_uuid))).scalar_one_or_none()
        if job is None or job.user_id != user.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    else:
        try:
            plan_uuid = uuid.UUID(body.content_plan_id)  # type: ignore[arg-type]
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="bad id") from exc
        plan = (
            await db.execute(select(ContentPlan).where(ContentPlan.id == plan_uuid))
        ).scalar_one_or_none()
        if plan is None or plan.user_id != user.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found")

    # One-thumb rule: replacing a prior thumb on the same video keeps a single row.
    if job_uuid is not None and body.signal in VIDEO_FEEDBACK_THUMB_SIGNALS:
        await db.execute(
            delete(VideoFeedback).where(
                VideoFeedback.user_id == user.id,
                VideoFeedback.job_id == job_uuid,
                VideoFeedback.signal.in_(VIDEO_FEEDBACK_THUMB_SIGNALS),
            )
        )

    # Explicit id (not just the column default) so the response carries it without a
    # post-commit refresh — the client needs it to toggle the reaction back off.
    row = VideoFeedback(
        id=uuid.uuid4(),
        user_id=user.id,
        job_id=job_uuid,
        content_plan_id=plan_uuid,
        signal=body.signal,
        note=note,
    )
    db.add(row)
    await db.commit()
    log.info("create_feedback", signal=body.signal, user_id=str(user.id))
    return FeedbackResponse(
        id=str(row.id),
        signal=row.signal,
        job_id=str(row.job_id) if row.job_id else None,
        content_plan_id=str(row.content_plan_id) if row.content_plan_id else None,
    )


@router.delete("/feedback/{feedback_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_feedback(
    feedback_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Remove a feedback row the caller owns (e.g. toggle a thumb off). 404 if it
    isn't the caller's — never leak that another user's feedback id exists."""
    try:
        fid = uuid.UUID(feedback_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="bad id") from exc
    row = (
        await db.execute(select(VideoFeedback).where(VideoFeedback.id == fid))
    ).scalar_one_or_none()
    if row is None or row.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feedback not found")
    await db.delete(row)
    await db.commit()
