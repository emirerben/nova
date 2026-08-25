"""Per-user "my" surface — the video library + one-off → plan attach (Phase 1 spine).

GET    /me/jobs                           — the signed-in user's videos (the library)
DELETE /me/jobs/{job_id}                   — delete one terminal video and its local media
POST   /me/jobs/{job_id}/add-to-plan      — pin a standalone video onto a plan day
POST   /me/jobs/{job_id}/open-in-editor   — promote a ready first cut into the editor
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
from urllib.parse import unquote, urlparse

import structlog
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentUser
from app.config import settings
from app.database import get_db
from app.models import (
    VIDEO_FEEDBACK_THUMB_SIGNALS,
    ContentPlan,
    Job,
    JobClip,
    JobStorageDeletion,
    OAuthToken,
    Persona,
    PlanItem,
    TikTokPublication,
    TrainingConsentEvent,
    User,
    VideoFeedback,
)
from app.services import tiktok_client
from app.services.content_plan_persona import (
    PLAN_PERSONA_OWNERSHIP_CONFLICT_DETAIL,
    PlanPersonaOwnershipError,
    load_owned_plan_persona,
)
from app.services.job_status import PLAN_ITEM_JOB_FAILED, PLAN_ITEM_JOB_READY
from app.services.token_crypto import decrypt_token
from app.storage import signed_download_url, signed_get_url

log = structlog.get_logger()
router = APIRouter()

# Job.status buckets — derived from the shared constants (plans/014 review) so
# the library tiles and the plan dashboard structurally agree on "ready"/
# "failed" across every job mode; the old hand-copied "lockstep" comment here
# is exactly how plan_items' copy drifted (missing template_ready/music_ready).
_JOB_READY = PLAN_ITEM_JOB_READY
_JOB_FAILED = PLAN_ITEM_JOB_FAILED
_DEFAULT_LIMIT = 24
_MAX_LIMIT = 60
OPEN_IN_EDITOR_NOT_READY_DETAIL = "Video is not ready to open in the editor."
OPEN_IN_EDITOR_LINK_CONFLICT_DETAIL = "Video is linked to a different plan item."
DELETE_JOB_NOT_TERMINAL_DETAIL = "This video is still being prepared or posted."

_DELETE_ACTIVE_TIKTOK_STATUSES = frozenset(
    {"queued", "snapshotting", "submitting", "processing", "submission_unknown"}
)
_DELETE_OUTPUT_PREFIXES = (
    "generative-jobs/{job_id}/",
    "jobs/{job_id}/",
    "music-jobs/{job_id}/",
    "auto-music-jobs/{job_id}/",
)
_DELETE_VARIANT_PATH_FIELDS = (
    "output_url",
    "video_path",
    "base_video_path",
    "subject_matte_path",
    "pre_media_overlay_video_path",
    "pre_sfx_video_path",
    "visual_blocks_base_path",
    "motion_base_path",
)


def _tiktok_delete_blocked(publication: TikTokPublication) -> bool:
    return publication.processing_status in _DELETE_ACTIVE_TIKTOK_STATUSES or (
        publication.processing_status == "failed" and publication.retryable
    )


# The persisted `output_url` (both per-variant and single-output job shapes) is a
# 1-day-TTL signed URL minted at render time; the underlying blob persists forever
# (see agents/DECISIONS.md "Storage retention"). Re-sign from the stored relative
# path on every read so the library grid never serves an expired signature past
# 24h — mirrors `PLAYBACK_URL_TTL_MIN` / `_variants_for_response` in
# routes/generative_jobs.py.
PLAYBACK_URL_TTL_MIN = 360


class TrainingConsentRequest(BaseModel):
    action: Literal["grant", "revoke"]
    terms_version: str = Field(min_length=1, max_length=100)
    idempotency_key: str = Field(min_length=1, max_length=128)


class TrainingConsentResponse(BaseModel):
    active: bool
    consent_event_id: str | None = None
    terms_version: str | None = None
    granted_at: str | None = None
    revoked_at: str | None = None


async def _latest_training_consent(
    db: AsyncSession,
    creator_id: uuid.UUID,
) -> TrainingConsentEvent | None:
    return (
        await db.execute(
            select(TrainingConsentEvent)
            .where(
                TrainingConsentEvent.creator_id == creator_id,
                TrainingConsentEvent.purpose == "edit_feedback_training",
            )
            .order_by(
                TrainingConsentEvent.effective_at.desc(),
                TrainingConsentEvent.created_at.desc(),
                TrainingConsentEvent.id.desc(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()


def _training_consent_out(event: TrainingConsentEvent | None) -> TrainingConsentResponse:
    if event is None:
        return TrainingConsentResponse(active=False)
    active = event.action == "grant"
    return TrainingConsentResponse(
        active=active,
        consent_event_id=str(event.id),
        terms_version=event.policy_version,
        granted_at=event.effective_at.isoformat() if active else None,
        revoked_at=event.effective_at.isoformat() if not active else None,
    )


@router.get("/training-consent", response_model=TrainingConsentResponse)
async def get_training_consent(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> TrainingConsentResponse:
    """Return the creator's current explicit edit-training consent state."""
    return _training_consent_out(await _latest_training_consent(db, user.id))


@router.post("/training-consent", response_model=TrainingConsentResponse)
async def set_training_consent(
    body: TrainingConsentRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> TrainingConsentResponse:
    """Append a grant/revoke event; revocation immediately excludes old artifacts."""
    key = body.idempotency_key.strip()
    existing = (
        await db.execute(
            select(TrainingConsentEvent).where(
                TrainingConsentEvent.creator_id == user.id,
                TrainingConsentEvent.idempotency_key == key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.action != body.action or existing.policy_version != body.terms_version:
            raise HTTPException(status_code=409, detail="consent idempotency key mismatch")
        return _training_consent_out(existing)

    latest = await _latest_training_consent(db, user.id)
    if body.action == "revoke" and (latest is None or latest.action != "grant"):
        return _training_consent_out(latest)
    row = TrainingConsentEvent(
        creator_id=user.id,
        purpose="edit_feedback_training",
        action=body.action,
        policy_version=body.terms_version.strip(),
        source="creator_settings",
        idempotency_key=key,
        revokes_consent_id=latest.id if body.action == "revoke" and latest else None,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    if body.action == "revoke" and latest is not None:
        from app.tasks.edit_training_artifacts import (  # noqa: PLC0415
            purge_edit_training_artifacts,
        )

        purge_edit_training_artifacts.delay(str(user.id), str(latest.id))
    return _training_consent_out(row)


async def _provision_editor_plan(db: AsyncSession, user: User) -> tuple[ContentPlan, bool]:
    """Provision the minimal plan graph needed by the footage-first editor.

    The user row serializes two first-time promotions. Re-checking for a plan
    after that lock makes provisioning idempotent without a new schema object.
    The boolean says whether the returned plan's persona is already locked and
    ownership-validated by construction.
    """
    locked_user = (
        await db.execute(
            select(User)
            .where(User.id == user.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if locked_user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    plan = (
        await db.execute(
            select(ContentPlan)
            .where(ContentPlan.user_id == user.id)
            .order_by(ContentPlan.created_at.desc())
            .limit(1)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if plan is not None:
        return plan, False

    persona = (
        await db.execute(
            select(Persona)
            .where(Persona.user_id == user.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if persona is None:
        persona = Persona(
            id=uuid.uuid4(),
            user_id=user.id,
            questionnaire={},
            persona={},
            persona_status="ready",
            idea_seeds=[],
        )
        db.add(persona)
        await db.flush()

    plan = ContentPlan(
        id=uuid.uuid4(),
        user_id=user.id,
        persona_id=persona.id,
        plan_status="ready",
        horizon_days=30,
        ownership_epoch=0,
    )
    db.add(plan)
    await db.flush()
    return plan, True


def _variant_rank(variant: dict, fallback: int) -> tuple[int, int]:
    """Stable rank key for task-owned variant dictionaries.

    Legacy rows can lack ``rank`` or contain a malformed value. Keep their list
    order after explicitly ranked variants without letting a bool masquerade as
    an integer rank.
    """
    raw = variant.get("rank")
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw, fallback
    return 1_000_000 + fallback, fallback


def _lowest_rank_ready_variant(job: Job) -> dict | None:
    variants = (job.assembly_plan or {}).get("variants")
    if not isinstance(variants, list):
        return None
    ready = [
        (index, variant)
        for index, variant in enumerate(variants)
        if isinstance(variant, dict)
        and variant.get("render_status") == "ready"
        and isinstance(variant.get("variant_id"), str)
        and variant["variant_id"].strip()
    ]
    if not ready:
        return None
    return min(ready, key=lambda pair: _variant_rank(pair[1], pair[0]))[1]


def _job_failure_metadata(job: Job) -> tuple[str | None, str | None]:
    """Return structured, UI-safe failure vocabulary without raw error detail."""
    reason = job.failure_reason if isinstance(job.failure_reason, str) else None
    variants = (job.assembly_plan or {}).get("variants")
    if not isinstance(variants, list):
        return reason, None
    failed = [
        (index, variant)
        for index, variant in enumerate(variants)
        if isinstance(variant, dict) and isinstance(variant.get("error_class"), str)
    ]
    if not failed:
        return reason, None
    variant = min(failed, key=lambda pair: _variant_rank(pair[1], pair[0]))[1]
    return reason, variant["error_class"]


def _derived_status(job: Job) -> str:
    """ready | generating | failed — derived from Job.status, never stored."""
    if job.status in _JOB_READY:
        return "ready"
    if job.status in _JOB_FAILED:
        return "failed"
    return "generating"


def _preview(job: Job) -> tuple[str | None, str | None, str | None]:
    """One playable URL for the library tile, across every job mode.

    Generative/content_plan jobs keep per-variant outputs in
    `assembly_plan["variants"][*]["output_url"]` (only "ready" variants have one);
    template/music jobs store a single `assembly_plan["output_url"]`.
    """
    if job.status == "cancelled":
        return None, None, None
    plan = job.assembly_plan or {}
    variants = plan.get("variants")
    if isinstance(variants, list):
        for v in variants:
            if v.get("render_status") == "ready" and v.get("output_url"):
                return (
                    v["output_url"],
                    str(v.get("variant_id") or "") or None,
                    v.get("video_path") if isinstance(v.get("video_path"), str) else None,
                )
        return None, None, None
    url = plan.get("output_url")
    output_path = plan.get("output_path")
    return (
        url if isinstance(url, str) else None,
        None,
        output_path if isinstance(output_path, str) else None,
    )


class LibraryTikTokPublication(BaseModel):
    id: str
    job_id: str
    variant_id: str | None
    delivery_mode: str
    processing_status: str
    visibility_status: str
    retryable: bool
    deletion_blocked: bool
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
    download_url: str | None = None
    output_variant_id: str | None = None
    tiktok_publishable: bool = False
    tiktok_publication: LibraryTikTokPublication | None = None
    created_at: datetime
    content_plan_item_id: str | None
    # Structured, allowlisted taxonomy only. Raw ``Job.error_detail`` and per-
    # variant ``error`` stay off this user-facing response.
    failure_reason: str | None = None
    error_class: str | None = None
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
    output_url, output_variant_id, output_path = _preview(job)
    if output_url and output_path:
        try:
            output_url = signed_get_url(output_path, PLAYBACK_URL_TTL_MIN)
        except Exception:  # noqa: BLE001 — a library row must survive signing failure
            log.warning(
                "library_playback_resign_failed",
                job_id=str(job.id),
                output_path=output_path,
                exc_info=True,
            )
    failure_reason, error_class = _job_failure_metadata(job)
    download_url: str | None = None
    if output_path:
        try:
            download_url = signed_download_url(
                output_path,
                f"kria-{str(job.id)[:8]}.mp4",
                expiration_minutes=360,
            )
        except Exception:  # noqa: BLE001 — a library row must survive signing failure
            log.warning("library_download_sign_failed", job_id=str(job.id), exc_info=True)
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
        download_url=download_url,
        output_variant_id=output_variant_id,
        tiktok_publishable=bool(output_url and has_owned_output),
        tiktok_publication=(
            LibraryTikTokPublication(
                id=str(tiktok_publication.id),
                job_id=str(tiktok_publication.job_id),
                variant_id=tiktok_publication.variant_id,
                delivery_mode=tiktok_publication.delivery_mode or "direct_post",
                processing_status=tiktok_publication.processing_status,
                visibility_status=tiktok_publication.visibility_status,
                retryable=tiktok_publication.retryable,
                deletion_blocked=_tiktok_delete_blocked(tiktok_publication),
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
        failure_reason=failure_reason,
        error_class=error_class,
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
    # Manual drafts are resumable through their plan item, but are not finished
    # videos and must never appear in the library before first export.
    q = select(Job).where(Job.user_id == user.id, Job.status != "draft")
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


def _normalize_job_storage_path(path: object) -> str | None:
    if not isinstance(path, str):
        return None
    candidate = path.strip().lstrip("/")
    if "://" in candidate:
        parsed = urlparse(candidate)
        bucket_prefix = f"/{settings.storage_bucket}/"
        if (
            parsed.scheme != "https"
            or parsed.netloc not in {"storage.googleapis.com", "storage.cloud.google.com"}
            or not parsed.path.startswith(bucket_prefix)
        ):
            return None
        candidate = unquote(parsed.path[len(bucket_prefix) :]).lstrip("/")
    if not candidate or ".." in candidate.split("/"):
        return None
    return candidate


def _job_output_path(path: object, job_id: uuid.UUID) -> str | None:
    candidate = _normalize_job_storage_path(path)
    if candidate is None:
        return None
    if any(
        candidate.startswith(prefix.format(job_id=job_id)) for prefix in _DELETE_OUTPUT_PREFIXES
    ):
        return candidate
    return None


def _job_source_path(path: object, *, user_id: uuid.UUID, job_id: uuid.UUID) -> str | None:
    candidate = _normalize_job_storage_path(path)
    if candidate is None:
        return None
    allowed_prefixes = (
        f"{user_id}/{job_id}/",
        f"dev-user/{job_id}/",
        f"dev-user/{user_id}/generative/",
        f"voiceover-uploads/direct/{user_id}/",
    )
    return candidate if candidate.startswith(allowed_prefixes) else None


def _job_input_paths(job: Job, *, user_id: uuid.UUID, linked_to_plan: bool) -> list[str]:
    if linked_to_plan:
        return []
    candidates = job.all_candidates if isinstance(job.all_candidates, dict) else {}
    raw_paths: list[object] = [job.raw_storage_path]
    clip_paths = candidates.get("clip_paths")
    if isinstance(clip_paths, list):
        raw_paths.extend(clip_paths)
    raw_paths.append(candidates.get("voiceover_gcs_path"))
    paths: list[str] = []
    for value in raw_paths:
        if path := _job_source_path(value, user_id=user_id, job_id=job.id):
            paths.append(path)
    return list(dict.fromkeys(paths))


def _shared_job_input_prefixes(user_id: uuid.UUID) -> tuple[str, ...]:
    return (
        f"dev-user/{user_id}/generative/",
        f"voiceover-uploads/direct/{user_id}/",
    )


def _job_storage_paths(
    job: Job,
    clips: list[JobClip],
    publications: list[TikTokPublication],
    *,
    user_id: uuid.UUID,
    linked_to_plan: bool | None = None,
) -> list[str]:
    """Collect exact, job-owned object keys without walking broad prefixes.

    Linked plan jobs deliberately skip raw input paths: those clips belong to
    the plan and remain recoverable after the finished render is deleted.
    """
    paths: list[str] = []

    def add_output(value: object) -> None:
        if path := _job_output_path(value, job.id):
            paths.append(path)

    for clip in clips:
        add_output(clip.video_path)
        add_output(clip.thumbnail_path)

    plan = job.assembly_plan if isinstance(job.assembly_plan, dict) else {}
    for field in ("output_path", "video_path", "output_url", "base_output_url"):
        add_output(plan.get(field))
    variants = plan.get("variants")
    if isinstance(variants, list):
        for variant in variants:
            if not isinstance(variant, dict):
                continue
            for field in _DELETE_VARIANT_PATH_FIELDS:
                value = variant.get(field)
                add_output(value)
                if field == "subject_matte_path":
                    if matte_path := _job_output_path(value, job.id):
                        if matte_path.endswith(".mp4"):
                            paths.append(f"{matte_path}.json")

    for publication in publications:
        add_output(publication.source_object_path)
        snapshot_path = f"tiktok-publish/{publication.id}.mp4"
        if publication.snapshot_object_path == snapshot_path:
            paths.append(snapshot_path)

    # `all_candidates.clip_paths` contains every source for template/music
    # jobs, while raw_storage_path is only the legacy/single-input fallback.
    # Never include either collection for a plan-linked job. The optional
    # override lets the route protect legacy rows where only PlanItem.current_job_id
    # carries the link and Job.content_plan_item_id is NULL.
    if linked_to_plan is None:
        linked_to_plan = job.content_plan_item_id is not None
    candidates = job.all_candidates if isinstance(job.all_candidates, dict) else {}
    clip_paths = candidates.get("clip_paths")
    if isinstance(clip_paths, list):
        # Timeline edits may copy plan footage into a job-owned generative-jobs
        # namespace. Those exact copies are safe to remove even for linked jobs;
        # the original users/{user_id}/plan/... inputs remain excluded below.
        for clip_path in clip_paths:
            add_output(clip_path)
    preprocessed_cache = candidates.get("preprocessed_source_cache")
    if isinstance(preprocessed_cache, dict):
        processed_clip_paths = preprocessed_cache.get("processed_clip_paths")
        if isinstance(processed_clip_paths, list):
            for cache_path in processed_clip_paths:
                add_output(cache_path)
    hdr_cache = candidates.get("hdr_pretonemap_cache")
    if isinstance(hdr_cache, dict):
        processed_by_clip_id = hdr_cache.get("processed_by_clip_id")
        if isinstance(processed_by_clip_id, dict):
            for cache_path in processed_by_clip_id.values():
                add_output(cache_path)

    paths.extend(_job_input_paths(job, user_id=user_id, linked_to_plan=linked_to_plan))

    return list(dict.fromkeys(paths))


async def _delete_job_storage_after_commit(outbox_id: uuid.UUID | None) -> None:
    """Best-effort dispatch; the committed outbox is the retry guarantee."""
    if outbox_id is None:
        return
    from app.tasks.account_lifecycle import purge_job_storage  # noqa: PLC0415

    try:
        purge_job_storage.apply_async(args=[str(outbox_id)])
    except Exception as exc:  # noqa: BLE001 — DB deletion must not be rolled back
        log.error(
            "purge_job_storage_dispatch_failed",
            outbox_id=str(outbox_id),
            error=str(exc),
        )


@router.delete("/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_my_job(
    job_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Delete one terminal video while keeping linked plan footage intact."""
    try:
        jid = uuid.UUID(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="bad id") from exc

    snapshot = (
        await db.execute(select(Job).where(Job.id == jid, Job.user_id == user.id))
    ).scalar_one_or_none()
    if snapshot is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    # Discover linked plan ids without locks first. The lock acquisition below
    # follows the repository-wide mutation order (ContentPlan -> Persona ->
    # PlanItem -> Job), so delete cannot deadlock with plan edits or attach.
    link_conditions = [PlanItem.current_job_id == jid]
    if snapshot.content_plan_item_id is not None:
        link_conditions.append(PlanItem.id == snapshot.content_plan_item_id)
    link_filter = link_conditions[0] if len(link_conditions) == 1 else or_(*link_conditions)
    linked_plan_ids = sorted(
        {
            plan_id
            for plan_id in (
                await db.execute(
                    select(PlanItem.content_plan_id)
                    .join(ContentPlan, ContentPlan.id == PlanItem.content_plan_id)
                    .where(ContentPlan.user_id == user.id, link_filter)
                )
            )
            .scalars()
            .all()
        },
        key=str,
    )
    linked_items: list[PlanItem] = []
    for plan_id in linked_plan_ids:
        plan = (
            await db.execute(
                select(ContentPlan)
                .where(ContentPlan.id == plan_id, ContentPlan.user_id == user.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if plan is None:
            continue
        try:
            await load_owned_plan_persona(db, plan, for_update=True)
        except PlanPersonaOwnershipError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=PLAN_PERSONA_OWNERSHIP_CONFLICT_DETAIL,
            ) from exc
        linked_items.extend(
            list(
                (
                    await db.execute(
                        select(PlanItem)
                        .where(PlanItem.content_plan_id == plan.id, link_filter)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                )
                .scalars()
                .all()
            )
        )

    locked_job = (
        await db.execute(
            select(Job)
            .where(Job.id == jid)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if locked_job is None or locked_job.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    if locked_job.status not in _JOB_READY | _JOB_FAILED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=DELETE_JOB_NOT_TERMINAL_DETAIL,
        )

    publications = list(
        (
            await db.execute(
                select(TikTokPublication)
                .where(TikTokPublication.job_id == jid, TikTokPublication.user_id == user.id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    if any(_tiktok_delete_blocked(publication) for publication in publications):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=DELETE_JOB_NOT_TERMINAL_DETAIL,
        )

    clips = list(
        (await db.execute(select(JobClip).where(JobClip.job_id == jid).with_for_update()))
        .scalars()
        .all()
    )
    linked_to_plan = bool(locked_job.content_plan_item_id or linked_items)
    object_paths = _job_storage_paths(
        locked_job,
        clips,
        publications,
        user_id=user.id,
        linked_to_plan=linked_to_plan,
    )
    shared_inputs = {
        path
        for path in _job_input_paths(locked_job, user_id=user.id, linked_to_plan=linked_to_plan)
        if path.startswith(_shared_job_input_prefixes(user.id))
    }
    if shared_inputs:
        shared_clip_clauses = [
            Job.raw_storage_path.in_(shared_inputs),
            Job.all_candidates["voiceover_gcs_path"].astext.in_(shared_inputs),
            *(Job.all_candidates["clip_paths"].contains([path]) for path in shared_inputs),
        ]
        referenced_rows = (
            await db.execute(
                select(Job.raw_storage_path, Job.all_candidates)
                .where(Job.user_id == user.id, Job.id != jid)
                .where(or_(*shared_clip_clauses))
            )
        ).all()
        referenced_inputs: set[str] = set()
        for raw_path, candidates in referenced_rows:
            if raw_path in shared_inputs:
                referenced_inputs.add(raw_path)
            if not isinstance(candidates, dict):
                continue
            if candidates.get("voiceover_gcs_path") in shared_inputs:
                referenced_inputs.add(candidates["voiceover_gcs_path"])
            candidate_clips = candidates.get("clip_paths")
            if isinstance(candidate_clips, list):
                referenced_inputs.update(set(candidate_clips) & shared_inputs)
        object_paths = [path for path in object_paths if path not in referenced_inputs]

    # Clear every forward plan pointer for this Job, including legacy rows
    # where Job.content_plan_item_id was not populated. The ownership join
    # prevents a malformed cross-user row from being touched.
    for item in linked_items:
        if item.current_job_id == jid:
            item.current_job_id = None

    locked_job.content_plan_item_id = None
    deletion_outbox_id: uuid.UUID | None = None
    if object_paths:
        deletion_outbox_id = uuid.uuid4()
        db.add(
            JobStorageDeletion(
                id=deletion_outbox_id,
                job_id=jid,
                object_paths=object_paths,
            )
        )
    await db.execute(
        delete(TikTokPublication).where(
            TikTokPublication.job_id == jid,
            TikTokPublication.user_id == user.id,
        )
    )
    await db.execute(delete(JobClip).where(JobClip.job_id == jid))
    await db.delete(locked_job)
    await db.commit()
    await _delete_job_storage_after_commit(deletion_outbox_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


class AddToPlanBody(BaseModel):
    day_index: int


class OpenInEditorBody(BaseModel):
    title: str | None = Field(default=None, max_length=500)


class OpenInEditorResponse(BaseModel):
    plan_item_id: str
    variant_id: str


class RetryJobResponse(BaseModel):
    job_id: str
    status: Literal["queued"] = "queued"


def _clean_optional_text(value: object, *, limit: int = 500) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()[:limit]
    return cleaned or None


@router.post("/jobs/{job_id}/retry", response_model=RetryJobResponse)
async def retry_failed_job(
    job_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> RetryJobResponse:
    """Retry a failed standalone first-cut job without duplicating its uploads."""
    try:
        jid = uuid.UUID(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="bad id") from exc

    snapshot = (await db.execute(select(Job).where(Job.id == jid))).scalar_one_or_none()
    if snapshot is None or snapshot.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    if snapshot.mode != "generative" or snapshot.content_plan_item_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This video cannot be retried from Create.",
        )

    locked_job = (
        await db.execute(
            select(Job)
            .where(Job.id == jid)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if locked_job is None or locked_job.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    if locked_job.mode != "generative" or locked_job.content_plan_item_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This video cannot be retried from Create.",
        )
    if locked_job.status not in (_JOB_FAILED - {"cancelled", "posting_failed"}):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only a failed video can be retried.",
        )

    locked_job.status = "queued"
    locked_job.error_detail = None
    locked_job.failure_reason = None
    locked_job.current_phase = None
    locked_job.worker_heartbeat_at = None
    locked_job.started_at = None
    locked_job.finished_at = None
    locked_job.celery_task_id = None
    locked_job.phase_log = []
    await db.commit()

    from app.services.job_dispatch import enqueue_orchestrator  # noqa: PLC0415
    from app.tasks.generative_build import orchestrate_generative_job  # noqa: PLC0415

    try:
        await enqueue_orchestrator(orchestrate_generative_job, locked_job.id, db)
    except Exception as exc:  # noqa: BLE001 — preserve a retryable terminal row
        await db.execute(
            update(Job)
            .where(Job.id == locked_job.id, Job.status == "queued")
            .values(
                status="processing_failed",
                failure_reason="dispatch_publish_failed",
                error_detail="The job couldn't be handed to the queue. Please try again.",
            )
        )
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The render queue is temporarily unavailable. Please try again.",
        ) from exc
    return RetryJobResponse(job_id=str(locked_job.id))


@router.post("/jobs/{job_id}/open-in-editor", response_model=OpenInEditorResponse)
async def open_job_in_editor(
    job_id: str,
    user: CurrentUser,
    body: OpenInEditorBody | None = None,
    db: AsyncSession = Depends(get_db),
) -> OpenInEditorResponse:
    """Promote a caller-owned ready first cut into the canonical plan editor.

    The operation is idempotent: once a Job is linked to a PlanItem, refreshes
    and duplicate requests return that item instead of creating another. Locks
    follow the shared mutation order ContentPlan -> Persona -> PlanItem(s) ->
    Job, so promotion cannot deadlock with plan edits or cancellation.
    """
    try:
        jid = uuid.UUID(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="bad id") from exc

    # Read-only ownership check before acquiring the wider plan lock set. The
    # Job is re-fetched FOR UPDATE at the end of the canonical lock order.
    snapshot = (await db.execute(select(Job).where(Job.id == jid))).scalar_one_or_none()
    if snapshot is None or snapshot.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    if snapshot.status == "cancelled" or _lowest_rank_ready_variant(snapshot) is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=OPEN_IN_EDITOR_NOT_READY_DETAIL,
        )

    if snapshot.content_plan_item_id:
        # Resolve an existing link without a write lock first, then lock its
        # ContentPlan separately. A joined FOR UPDATE would lock PlanItem before
        # Persona and violate the repository's canonical lock order.
        linked_plan_id = (
            await db.execute(
                select(PlanItem.content_plan_id)
                .join(ContentPlan, ContentPlan.id == PlanItem.content_plan_id)
                .where(
                    ContentPlan.user_id == user.id,
                    PlanItem.id == snapshot.content_plan_item_id,
                )
            )
        ).scalar_one_or_none()
        if linked_plan_id is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No content plan to open the video in",
            )
        # Existing links win idempotently, including links to an older plan.
        plan_stmt = (
            select(ContentPlan)
            .where(
                ContentPlan.user_id == user.id,
                ContentPlan.id == linked_plan_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    else:
        plan_stmt = (
            select(ContentPlan)
            .where(ContentPlan.user_id == user.id)
            .order_by(ContentPlan.created_at.desc())
            .limit(1)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    plan = (await db.execute(plan_stmt)).scalar_one_or_none()
    persona_already_locked = False
    if plan is None and snapshot.content_plan_item_id is None:
        plan, persona_already_locked = await _provision_editor_plan(db, user)
    if plan is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No content plan to open the video in",
        )
    if not persona_already_locked:
        try:
            await load_owned_plan_persona(db, plan, for_update=True)
        except PlanPersonaOwnershipError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=PLAN_PERSONA_OWNERSHIP_CONFLICT_DETAIL,
            ) from exc

    items_stmt = (
        select(PlanItem)
        .where(PlanItem.content_plan_id == plan.id)
        .order_by(PlanItem.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    plan_items = list((await db.execute(items_stmt)).scalars().all())
    items_by_id = {item.id: item for item in plan_items}

    locked_job = (
        await db.execute(
            select(Job)
            .where(Job.id == jid)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if locked_job is None or locked_job.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    ready_variant = _lowest_rank_ready_variant(locked_job)
    if locked_job.status == "cancelled" or ready_variant is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=OPEN_IN_EDITOR_NOT_READY_DETAIL,
        )

    if locked_job.content_plan_item_id:
        existing_item = items_by_id.get(locked_job.content_plan_item_id)
        if existing_item is None:
            # The link changed to a different plan between the snapshot and the
            # Job lock. Never create a duplicate or lock PlanItem after Job.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=OPEN_IN_EDITOR_LINK_CONFLICT_DETAIL,
            )
        if existing_item.current_job_id not in (None, locked_job.id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=OPEN_IN_EDITOR_LINK_CONFLICT_DETAIL,
            )
        needs_commit = False
        if existing_item.current_job_id is None:
            existing_item.current_job_id = locked_job.id
            needs_commit = True
        if locked_job.content_plan_ownership_epoch != plan.ownership_epoch:
            locked_job.content_plan_ownership_epoch = plan.ownership_epoch
            needs_commit = True
        if locked_job.mode != "content_plan":
            locked_job.mode = "content_plan"
            needs_commit = True
        if needs_commit:
            await db.commit()
        return OpenInEditorResponse(
            plan_item_id=str(existing_item.id),
            variant_id=ready_variant["variant_id"],
        )

    candidates = locked_job.all_candidates or {}
    if not isinstance(candidates, dict):
        candidates = {}
    raw_clip_paths = candidates.get("clip_paths")
    if not isinstance(raw_clip_paths, list):
        raw_clip_paths = []
    clip_paths = [path for path in raw_clip_paths if isinstance(path, str) and path.strip()]
    persona_context = candidates.get("persona")
    if not isinstance(persona_context, dict):
        persona_context = {}
    requested_title = _clean_optional_text(body.title if body else None)
    item_theme = requested_title or _clean_optional_text(persona_context.get("theme"))
    item_idea = (
        requested_title
        or _clean_optional_text(persona_context.get("idea"))
        or item_theme
        or "Kria first cut"
    )
    clip_notes = candidates.get("clip_notes")
    if not isinstance(clip_notes, dict):
        clip_notes = {}
    assignments = [
        {
            "gcs_path": path,
            "shot_id": None,
            **(
                {"user_note": note.strip()[:200]}
                if isinstance((note := clip_notes.get(path)), str) and note.strip()
                else {}
            ),
        }
        for path in clip_paths
    ]
    next_position = max((item.position for item in plan_items), default=0) + 1
    item_id = uuid.uuid4()
    item = PlanItem(
        id=item_id,
        content_plan_id=plan.id,
        day_index=None,
        position=next_position,
        theme=item_theme,
        idea=item_idea,
        notes=_clean_optional_text(persona_context.get("idea")),
        item_status="idea",
        content_mode="existing_footage",
        edit_format=_clean_optional_text(candidates.get("edit_format"), limit=100) or "montage",
        montage_preset=(
            _clean_optional_text(candidates.get("montage_preset"), limit=100) or "classic"
        ),
        landscape_fit=(_clean_optional_text(candidates.get("landscape_fit"), limit=20) or "fill"),
        clip_gcs_paths=clip_paths,
        clip_assignments=assignments,
        filming_guide=(
            list(candidates.get("filming_guide") or [])
            if isinstance(candidates.get("filming_guide"), list)
            else []
        ),
        voiceover_gcs_path=_clean_optional_text(candidates.get("voiceover_gcs_path"), limit=2_000),
        audio_mode="voiceover" if candidates.get("voiceover_gcs_path") else "kria",
        voiceover_bed_level=(
            float(candidates["voiceover_bed_level"])
            if isinstance(candidates.get("voiceover_bed_level"), (int, float))
            and not isinstance(candidates.get("voiceover_bed_level"), bool)
            else None
        ),
        voiceover_caption_style=(
            candidates.get("voiceover_caption_style")
            if candidates.get("voiceover_caption_style") == "word"
            else None
        ),
        current_job_id=locked_job.id,
        user_edited=True,
    )
    db.add(item)
    # The EditorShell render path is guarded as plan-owned. Transition the
    # standalone job at the same atomic boundary as both foreign-key links so
    # its first Save cannot be rejected by the worker ownership fence.
    locked_job.mode = "content_plan"
    locked_job.content_plan_item_id = item.id
    locked_job.content_plan_ownership_epoch = plan.ownership_epoch
    await db.commit()
    log.info(
        "open_job_in_editor",
        job_id=job_id,
        plan_item_id=str(item.id),
        variant_id=ready_variant["variant_id"],
        user_id=str(user.id),
    )
    return OpenInEditorResponse(
        plan_item_id=str(item.id),
        variant_id=ready_variant["variant_id"],
    )


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
            .with_for_update()
        )
    ).scalar_one_or_none()
    if plan is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No content plan to add to"
        )
    try:
        await load_owned_plan_persona(db, plan, for_update=True)
    except PlanPersonaOwnershipError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=PLAN_PERSONA_OWNERSHIP_CONFLICT_DETAIL,
        ) from exc

    item = (
        await db.execute(
            select(PlanItem)
            .where(
                PlanItem.content_plan_id == plan.id,
                PlanItem.day_index == body.day_index,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan day not found")

    # Lock order is Plan -> Persona -> PlanItem -> Job. Re-fetching the Job at
    # the end makes cancellation and this attachment one atomic winner without
    # holding the Job lock while resolving the plan target.
    locked_job = (
        await db.execute(select(Job).where(Job.id == jid).with_for_update())
    ).scalar_one_or_none()
    if locked_job is None or locked_job.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    if locked_job.status == "cancelled":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cancelled videos cannot be added to a plan.",
        )

    item.current_job_id = locked_job.id
    locked_job.content_plan_item_id = item.id
    await db.commit()
    log.info("add_job_to_plan", job_id=job_id, day_index=body.day_index, user_id=str(user.id))
    return _to_library_job(locked_job, content_plan_item_id=str(item.id))


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
            await db.execute(
                select(ContentPlan).where(ContentPlan.id == plan_uuid).with_for_update()
            )
        ).scalar_one_or_none()
        if plan is None or plan.user_id != user.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found")
        try:
            await load_owned_plan_persona(db, plan, for_update=True)
        except PlanPersonaOwnershipError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=PLAN_PERSONA_OWNERSHIP_CONFLICT_DETAIL,
            ) from exc

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
    # Export is still a read boundary: a quarantined or cross-owner plan must
    # not be serialized merely because this endpoint does not otherwise need
    # Persona fields.  Validate every parent before loading any child state.
    try:
        for plan in plans:
            await load_owned_plan_persona(db, plan)
    except PlanPersonaOwnershipError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=PLAN_PERSONA_OWNERSHIP_CONFLICT_DETAIL,
        ) from exc
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
                # Cancellation makes rendered output references private even in
                # the account export; retain the Job audit metadata around it.
                "assembly_plan": None if job.status == "cancelled" else job.assembly_plan,
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
                "delivery_mode": t.delivery_mode or "direct_post",
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
