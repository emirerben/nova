"""Per-user "my" surface — the video library + one-off → plan attach (Phase 1 spine).

GET    /me/jobs                       — the signed-in user's videos (the library)
POST   /me/jobs/{job_id}/add-to-plan  — pin a standalone video onto a plan day
GET    /me/export                     — data-portability bundle (privacy policy §9)
POST   /me/account/delete-request     — step 1/2 of account erasure: emails a code
POST   /me/account/delete-confirm     — step 2/2: verify the code, permanently erase

STRICT auth only: every endpoint uses `CurrentUser` (never `CurrentUserOrSynthetic`),
so the user scope comes from the validated `X-User-Id` header — never a client param.
There is no `user_id` query input to forge, so the list is IDOR-safe by construction;
cross-user references on add-to-plan return 404 (not 403) so we don't leak which ids exist.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

import structlog
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentUser
from app.config import settings
from app.database import get_db
from app.models import (
    VIDEO_FEEDBACK_THUMB_SIGNALS,
    ContentPlan,
    Job,
    OAuthToken,
    Persona,
    PlanItem,
    TikTokPublication,
    User,
    VideoFeedback,
)
from app.services import tiktok_client
from app.services.token_crypto import decrypt_token
from app.storage import signed_get_url

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


def _preview(job: Job) -> tuple[str | None, str | None]:
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
                return v["output_url"], str(v.get("variant_id") or "") or None
        return None, None
    url = plan.get("output_url")
    return (url if isinstance(url, str) else None), None


class LibraryTikTokPublication(BaseModel):
    id: str
    job_id: str
    variant_id: str | None
    processing_status: str
    visibility_status: str
    retryable: bool
    failure_code: str | None
    failure_detail: str | None
    latest_metrics: dict | None
    metrics_synced_at: datetime | None
    created_at: datetime
    updated_at: datetime


def _job_mode(job: Job) -> str:
    # `mode` is the Phase-3 discriminator; fall back to the legacy job_type.
    return job.mode or job.job_type or "default"


class LibraryJob(BaseModel):
    id: str
    mode: str  # generative | content_plan | template | music | auto_music | default
    status: str  # derived: ready | generating | failed
    raw_status: str
    output_url: str | None
    output_variant_id: str | None = None
    tiktok_publishable: bool = False
    tiktok_publication: LibraryTikTokPublication | None = None
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
    tiktok_publication: TikTokPublication | None = None,
) -> LibraryJob:
    output_url, output_variant_id = _preview(job)
    plan = job.assembly_plan or {}
    has_owned_output = bool(
        output_variant_id
        or plan.get("output_path")
        or _job_mode(job) in {"template", "music", "auto_music"}
    )
    return LibraryJob(
        id=str(job.id),
        mode=_job_mode(job),
        status=_derived_status(job),
        raw_status=job.status,
        output_url=output_url,
        output_variant_id=output_variant_id,
        tiktok_publishable=bool(output_url and has_owned_output),
        tiktok_publication=(
            LibraryTikTokPublication(
                id=str(tiktok_publication.id),
                job_id=str(tiktok_publication.job_id),
                variant_id=tiktok_publication.variant_id,
                processing_status=tiktok_publication.processing_status,
                visibility_status=tiktok_publication.visibility_status,
                retryable=tiktok_publication.retryable,
                failure_code=tiktok_publication.failure_code,
                failure_detail=tiktok_publication.failure_detail,
                latest_metrics=tiktok_publication.latest_metrics,
                metrics_synced_at=tiktok_publication.metrics_synced_at,
                created_at=tiktok_publication.created_at,
                updated_at=tiktok_publication.updated_at,
            )
            if tiktok_publication
            else None
        ),
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
    latest_tiktok: dict[uuid.UUID, TikTokPublication] = {}
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

        publication_rows = (
            (
                await db.execute(
                    select(TikTokPublication)
                    .where(
                        TikTokPublication.user_id == user.id,
                        TikTokPublication.job_id.in_([j.id for j in rows]),
                    )
                    .order_by(TikTokPublication.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        for publication in publication_rows:
            latest_tiktok.setdefault(publication.job_id, publication)

    return LibraryResponse(
        jobs=[
            _to_library_job(
                j,
                feedback_signal=thumbs.get(j.id),
                tiktok_publication=latest_tiktok.get(j.id),
            )
            for j in rows
        ],
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


# ── Data export (privacy policy §9 — "Export" right) ──────────────────────────

# How long a re-signed source-media URL in the export bundle stays valid. Longer
# than the default 5-minute probe TTL (storage.signed_get_url) since this is a
# "download your data" link a user may not open immediately.
_EXPORT_SIGNED_URL_MINUTES = 60
# Signing is a per-job network round-trip; cap it so an account with thousands of
# jobs can't turn this into a multi-minute request. Every job's METADATA is still
# included in full — only the signed source-media link is capped, and the
# response says so explicitly (no silent truncation).
_EXPORT_MAX_SIGNED_MEDIA = 100


class ExportResponse(BaseModel):
    exported_at: datetime
    user: dict[str, Any]
    persona: dict[str, Any] | None
    content_plans: list[dict[str, Any]]
    jobs: list[dict[str, Any]]
    feedback: list[dict[str, Any]]
    tiktok_publications: list[dict[str, Any]]
    note: str


@router.get("/export", response_model=ExportResponse)
async def export_my_data(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ExportResponse:
    """Everything Kria holds about the caller, as one JSON bundle.

    Synchronous rather than the async "email a download link" pattern used
    elsewhere (e.g. music test-job renders) — this is metadata plus a bounded
    set of re-signed links, not a multi-gigabyte media archive, so a direct
    authenticated response is simpler and just as correct. Scope note: this
    exports job METADATA and a signed link to each job's original uploaded
    source (capped at _EXPORT_MAX_SIGNED_MEDIA, see above); it does not
    re-derive every render pipeline's rendered-output URL contract across all
    five job modes — those are already reachable via the existing library UI
    while the account is active.
    """
    persona_row = (
        await db.execute(select(Persona).where(Persona.user_id == user.id))
    ).scalar_one_or_none()

    plans = (
        (await db.execute(select(ContentPlan).where(ContentPlan.user_id == user.id)))
        .scalars()
        .all()
    )
    plan_ids = [p.id for p in plans]
    items_by_plan: dict[uuid.UUID, list[PlanItem]] = {pid: [] for pid in plan_ids}
    if plan_ids:
        items = (
            (await db.execute(select(PlanItem).where(PlanItem.content_plan_id.in_(plan_ids))))
            .scalars()
            .all()
        )
        for item in items:
            items_by_plan.setdefault(item.content_plan_id, []).append(item)

    jobs = (
        (
            await db.execute(
                select(Job).where(Job.user_id == user.id).order_by(Job.created_at.desc())
            )
        )
        .scalars()
        .all()
    )

    feedback_rows = (
        (
            await db.execute(
                select(VideoFeedback)
                .where(VideoFeedback.user_id == user.id)
                .order_by(VideoFeedback.created_at.desc())
            )
        )
        .scalars()
        .all()
    )

    tiktok_rows = (
        (
            await db.execute(
                select(TikTokPublication)
                .where(TikTokPublication.user_id == user.id)
                .order_by(TikTokPublication.created_at.desc())
            )
        )
        .scalars()
        .all()
    )

    jobs_out: list[dict[str, Any]] = []
    signed_count = 0
    for job in jobs:
        source_url: str | None = None
        if job.raw_storage_path and signed_count < _EXPORT_MAX_SIGNED_MEDIA:
            try:
                source_url = signed_get_url(
                    job.raw_storage_path, expiration_minutes=_EXPORT_SIGNED_URL_MINUTES
                )
                signed_count += 1
            except Exception:  # noqa: BLE001 — a signing hiccup shouldn't fail the export
                source_url = None
        jobs_out.append(
            {
                "id": str(job.id),
                "mode": job.mode or job.job_type,
                "status": job.status,
                "created_at": job.created_at.isoformat(),
                "transcript": job.transcript,
                "selected_platforms": job.selected_platforms,
                "assembly_plan": job.assembly_plan,
                "source_media_url": source_url,
            }
        )
    media_truncated = len(jobs) > _EXPORT_MAX_SIGNED_MEDIA

    log.info("export_my_data", user_id=str(user.id), job_count=len(jobs), plan_count=len(plans))
    return ExportResponse(
        exported_at=datetime.now(),
        user={
            "id": str(user.id),
            "email": user.email,
            "name": user.name,
            "auth_provider": user.auth_provider,
            "onboarding_status": user.onboarding_status,
            "created_at": user.created_at.isoformat(),
        },
        persona=(
            {
                "questionnaire": persona_row.questionnaire,
                "persona": persona_row.persona,
                "tiktok_profile": persona_row.tiktok_profile,
                "style": persona_row.style,
                "idea_seeds": persona_row.idea_seeds,
                "persona_status": persona_row.persona_status,
                "created_at": persona_row.created_at.isoformat(),
            }
            if persona_row
            else None
        ),
        content_plans=[
            {
                "id": str(plan.id),
                "plan_status": plan.plan_status,
                "horizon_days": plan.horizon_days,
                "start_date": plan.start_date.isoformat() if plan.start_date else None,
                "events": plan.events,
                "preference_summary": plan.preference_summary,
                "items": [
                    {
                        "id": str(item.id),
                        "day_index": item.day_index,
                        "idea": item.idea,
                        "theme": item.theme,
                        "filming_suggestion": item.filming_suggestion,
                        "rationale": item.rationale,
                        "edit_format": item.edit_format,
                        "scheduled_date": (
                            item.scheduled_date.isoformat() if item.scheduled_date else None
                        ),
                        "notes": item.notes,
                        "scenes": item.scenes,
                        "clip_gcs_paths": item.clip_gcs_paths,
                        "voiceover_script": item.voiceover_script,
                    }
                    for item in items_by_plan.get(plan.id, [])
                ],
            }
            for plan in plans
        ],
        jobs=jobs_out,
        feedback=[
            {
                "signal": f.signal,
                "note": f.note,
                "job_id": str(f.job_id) if f.job_id else None,
                "content_plan_id": str(f.content_plan_id) if f.content_plan_id else None,
                "created_at": f.created_at.isoformat(),
            }
            for f in feedback_rows
        ],
        tiktok_publications=[
            {
                "id": str(t.id),
                "job_id": str(t.job_id),
                "tiktok_post_id": t.tiktok_post_id,
                "processing_status": t.processing_status,
                "visibility_status": t.visibility_status,
                "latest_metrics": t.latest_metrics,
                "created_at": t.created_at.isoformat(),
            }
            for t in tiktok_rows
        ],
        note=(
            f"{signed_count} of {len(jobs)} jobs include a re-signed source-media link"
            f" (capped at {_EXPORT_MAX_SIGNED_MEDIA})."
            if media_truncated
            else "All jobs include a re-signed source-media link where available."
        ),
    )


# ── Account deletion (privacy policy §9 — "Delete" right) ─────────────────────

# Confirmation codes are stateless Fernet tokens of the caller's own user id —
# nothing is persisted server-side between request and confirm. Fernet embeds
# its own timestamp, so `.decrypt(token, ttl=...)` enforces expiry without a
# separate expires_at column. Reuses TOKEN_ENCRYPTION_KEY (already required
# infra for OAuthToken encryption, see services/token_crypto.py) rather than
# adding a second secret to provision.
_ACCOUNT_DELETE_TTL_SECONDS = 3600


def _account_delete_fernet() -> Fernet:
    if not settings.token_encryption_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Account deletion is not configured",
        )
    try:
        return Fernet(settings.token_encryption_key.encode())
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Account deletion is not configured",
        ) from exc


class DeleteRequestResponse(BaseModel):
    requested: bool


@router.post(
    "/account/delete-request",
    response_model=DeleteRequestResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_account_deletion(user: CurrentUser) -> DeleteRequestResponse:
    """Step 1 of 2: email the caller a one-time confirmation code.

    No DB write here — the code is minted fresh from the caller's own id, so a
    user can request it as many times as they like without any cleanup concern.
    """
    from app.tasks.account_lifecycle import send_account_deletion_email  # noqa: PLC0415

    token = _account_delete_fernet().encrypt(str(user.id).encode()).decode()
    send_account_deletion_email.delay(user.email, token)
    log.info("account_deletion_requested", user_id=str(user.id))
    return DeleteRequestResponse(requested=True)


class DeleteConfirmBody(BaseModel):
    token: str


@router.post("/account/delete-confirm", status_code=status.HTTP_204_NO_CONTENT)
async def confirm_account_deletion(
    body: DeleteConfirmBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Step 2 of 2: verify the emailed code, then permanently erase the account.

    Requires the caller to still be signed in as the SAME user the code was
    issued to — the decrypted token must equal str(user.id), so a leaked code
    alone can't be used to delete a different account. 404 (not 403) on a
    mismatch, matching this file's IDOR convention elsewhere.

    Full erasure — unlike POST /personas/reset, which explicitly KEEPS jobs.
    DB rows are deleted synchronously in FK-safe order; none of Job, OAuthToken,
    or TikTokPublication cascade from users.id at the DB level (see
    docs/legal/README.md), so each needs an explicit step before the final
    `DELETE FROM users` can succeed — the same hazard reset_persona documents
    for plan_items, extended here:

      1. Null Job.content_plan_item_id (no ondelete — blocks the content_plan
         cascade below otherwise).
      2. Best-effort revoke + delete TikTok OAuth tokens and delete
         TikTokPublication rows (TikTokPublication.job_id has no ondelete —
         must go before jobs).
      3. Delete remaining OAuthToken rows (no ondelete from users.id).
      4. Delete Job rows (no ondelete from users.id either; cascades AgentRun
         and VideoFeedback automatically via their own ondelete=CASCADE).
      5. Delete the User row — Persona, ContentPlan (→ PlanItem →
         PlanItemAsset), and any remaining VideoFeedback all cascade from here
         via direct ondelete=CASCADE FKs to users.id.

    GCS bytes are swept afterward by tasks.purge_user_storage — see that
    task's docstring for why it's async and why the ids are captured here
    first (the DB row is gone by the time that task runs).
    """
    try:
        plaintext = (
            _account_delete_fernet()
            .decrypt(body.token.encode(), ttl=_ACCOUNT_DELETE_TTL_SECONDS)
            .decode()
        )
    except InvalidToken as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired confirmation code — request a new one",
        ) from exc
    if plaintext != str(user.id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Confirmation code does not match the signed-in account",
        )

    jobs = (await db.execute(select(Job).where(Job.user_id == user.id))).scalars().all()
    job_ids = [str(j.id) for j in jobs]
    raw_paths = [j.raw_storage_path for j in jobs if j.raw_storage_path]

    # 1. Sever job → plan_item back-refs before the content_plan cascade fires.
    await db.execute(
        update(Job)
        .where(Job.user_id == user.id, Job.content_plan_item_id.is_not(None))
        .values(content_plan_item_id=None)
    )
    # 2. Revoke + clear TikTok connection, delete publication rows.
    tiktok_tokens = (
        (
            await db.execute(
                select(OAuthToken).where(
                    OAuthToken.user_id == user.id, OAuthToken.platform == "tiktok"
                )
            )
        )
        .scalars()
        .all()
    )
    for token_row in tiktok_tokens:
        if token_row.access_token:
            try:
                await run_in_threadpool(
                    tiktok_client.revoke_access, decrypt_token(token_row.access_token)
                )
            except Exception:  # noqa: BLE001 — local erasure must still proceed
                pass
    await db.execute(delete(TikTokPublication).where(TikTokPublication.user_id == user.id))
    # 3. Remaining OAuth tokens (instagram/youtube — no revoke API wired yet).
    await db.execute(delete(OAuthToken).where(OAuthToken.user_id == user.id))
    # 4. Jobs — cascades AgentRun + VideoFeedback automatically.
    await db.execute(delete(Job).where(Job.user_id == user.id))
    # 5. The user row — cascades Persona/ContentPlan/PlanItem/PlanItemAsset/
    #    any remaining VideoFeedback.
    await db.execute(delete(User).where(User.id == user.id))
    await db.commit()

    from app.tasks.account_lifecycle import purge_user_storage  # noqa: PLC0415

    purge_user_storage.delay(str(user.id), job_ids, raw_paths)

    log.info("account_deleted", user_id=str(user.id), job_count=len(job_ids))
    return Response(status_code=204)
