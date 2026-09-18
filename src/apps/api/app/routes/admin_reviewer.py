"""Admin seed endpoint for the Apple Beta App Review demo account (KRI-111).

Apple's App Review team signs in with a fixed reviewer email/password (see
app/services/reviewer_login.py). A brand-new account has an empty library,
which reads as a broken app during review, so this endpoint clones a handful
of a real, known-good user's finished jobs onto the reviewer account.

Read-only against the source user — every write lands on the reviewer
account only. Idempotent: re-running with the same source user skips any
source job that was already cloned (`reviewer_seed_source_job_id` marker on
the clone's `assembly_plan`).
"""

from __future__ import annotations

import copy
import uuid
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.config import settings
from app.database import get_db
from app.models import Job, User
from app.routes.admin import _require_admin
from app.services.job_status import PLAN_ITEM_JOB_READY
from app.services.job_storage_paths import JOB_OUTPUT_PREFIXES, owned_job_output_path
from app.services.reviewer_login import is_configured

log = structlog.get_logger(__name__)
router = APIRouter(dependencies=[Depends(_require_admin)])

# The only assembly_plan fields `routes/me.py::_preview()` runs through the
# ownership check (`owned_job_output_path`) before signing — see that
# function and `_to_library_job()`. Any other field (transcript, probe
# metadata, etc.) is inert JSON that doesn't need rewriting.
_SOURCE_FIELDS_TOP = ("output_path", "video_path", "output_url", "poster_path")
_SOURCE_FIELDS_VARIANT = ("video_path", "output_url", "poster_path")

# Columns that encode a cross-row reference rather than plain job data.
# Copying them verbatim onto a clone owned by a *different* user would either
# point at another account's PlanItem (content_plan_item_id /
# content_plan_ownership_epoch) or at a Celery task that belongs to the
# SOURCE job (celery_task_id) — the admin job-debug view can revoke a task by
# id, and revoking the source user's real in-flight/finished task through the
# clone would be a real (if narrow) blast-radius bug. worker_heartbeat_at is
# a liveness beacon for an orchestrator run that never happened for the
# clone. All four are deliberately left NULL instead of copied.
_NULLED_REFERENCE_COLUMNS = (
    "content_plan_item_id",
    "content_plan_ownership_epoch",
    "celery_task_id",
    "worker_heartbeat_at",
)


class SeedReviewerAccountRequest(BaseModel):
    source_user_email: EmailStr
    limit: int = Field(default=6, ge=1, le=25)


class SeedReviewerAccountResponse(BaseModel):
    reviewer_user_id: str
    created_user: bool
    cloned: list[str]
    skipped: list[str]


def _clone_owned_output_path(
    value: object, old_job: Job, new_job_id: uuid.UUID, new_user_id: uuid.UUID
) -> str | None:
    """Server-side copy an owned object to a key scoped by the CLONE's job id.

    `owned_job_output_path` — the same check `routes/me.py` runs before
    signing a playback URL — is gated on the owning job's own id (e.g.
    ``generative-jobs/{job_id}/...``), not just the owning user's id. A
    cloned Job row gets a brand-new uuid, so literally reusing the source
    job's stored paths would make every cloned video look "ownerless" to
    that check, and the library tile would never resolve a playable URL —
    read-time re-signing in `me.py` has nothing to sign. Copying the
    underlying object to a destination key scoped by the new job id (and
    re-pointing the JSON at the copy) keeps that ownership check — and
    therefore the existing re-signing path — working unmodified for the
    clone. Best-effort: a copy failure leaves the field untouched rather than
    failing the whole seed.
    """
    candidate = owned_job_output_path(value, old_job)
    if candidate is None:
        return None
    destination: str | None = None
    for prefix in JOB_OUTPUT_PREFIXES:
        old_prefix = prefix.format(job_id=old_job.id)
        if candidate.startswith(old_prefix):
            destination = prefix.format(job_id=new_job_id) + candidate[len(old_prefix) :]
            break
    if destination is None:
        legacy_prefix = f"{old_job.user_id}/{old_job.id}/"
        if candidate.startswith(legacy_prefix):
            destination = f"{new_user_id}/{new_job_id}/" + candidate[len(legacy_prefix) :]
    if destination is None or destination == candidate:
        return None
    try:
        storage.copy_object(candidate, destination)
    except Exception:
        log.warning(
            "admin_reviewer.seed.copy_object_failed",
            src=candidate,
            job_id=str(old_job.id),
            exc_info=True,
        )
        return None
    return destination


def _clone_assembly_plan(
    plan: object, old_job: Job, new_job_id: uuid.UUID, new_user_id: uuid.UUID
) -> dict[str, Any]:
    new_plan: dict[str, Any] = copy.deepcopy(plan) if isinstance(plan, dict) else {}
    for field in _SOURCE_FIELDS_TOP:
        if field in new_plan:
            cloned = _clone_owned_output_path(new_plan.get(field), old_job, new_job_id, new_user_id)
            if cloned:
                new_plan[field] = cloned
    variants = new_plan.get("variants")
    if isinstance(variants, list):
        for variant in variants:
            if not isinstance(variant, dict):
                continue
            for field in _SOURCE_FIELDS_VARIANT:
                if field in variant:
                    cloned = _clone_owned_output_path(
                        variant.get(field), old_job, new_job_id, new_user_id
                    )
                    if cloned:
                        variant[field] = cloned
    # Idempotency marker read back by this endpoint on every call.
    new_plan["reviewer_seed_source_job_id"] = str(old_job.id)
    return new_plan


def _clone_job(source_job: Job, reviewer: User) -> Job:
    new_id = uuid.uuid4()
    new_plan = _clone_assembly_plan(source_job.assembly_plan, source_job, new_id, reviewer.id)
    return Job(
        id=new_id,
        user_id=reviewer.id,
        status=source_job.status,
        job_type=source_job.job_type,
        mode=source_job.mode,
        template_id=source_job.template_id,
        music_track_id=source_job.music_track_id,
        assembly_plan=new_plan,
        raw_storage_path=source_job.raw_storage_path,
        selected_platforms=source_job.selected_platforms,
        probe_metadata=source_job.probe_metadata,
        transcript=source_job.transcript,
        scene_cuts=source_job.scene_cuts,
        all_candidates=source_job.all_candidates,
        error_detail=source_job.error_detail,
        failure_reason=source_job.failure_reason,
        current_phase=source_job.current_phase,
        phase_log=copy.deepcopy(source_job.phase_log) if source_job.phase_log else [],
        pipeline_trace=copy.deepcopy(source_job.pipeline_trace)
        if source_job.pipeline_trace
        else None,
        content_plan_item_id=None,
        content_plan_ownership_epoch=None,
        celery_task_id=None,
        started_at=source_job.started_at,
        finished_at=source_job.finished_at,
        worker_heartbeat_at=None,
        created_at=source_job.created_at,
    )


@router.post("/seed", response_model=SeedReviewerAccountResponse)
async def seed_reviewer_account(
    body: SeedReviewerAccountRequest,
    db: AsyncSession = Depends(get_db),
) -> SeedReviewerAccountResponse:
    if not is_configured():
        raise HTTPException(status_code=404, detail="Not found")

    source = (
        await db.execute(
            select(User).where(
                func.lower(User.email) == str(body.source_user_email).strip().lower()
            )
        )
    ).scalar_one_or_none()
    if source is None:
        raise HTTPException(status_code=404, detail="source_user_not_found")

    reviewer_email = settings.reviewer_login_email.strip().lower()
    reviewer = (
        await db.execute(select(User).where(func.lower(User.email) == reviewer_email))
    ).scalar_one_or_none()
    created_user = False
    if reviewer is None:
        reviewer = User(
            id=uuid.uuid4(),
            email=settings.reviewer_login_email.strip(),
            name="Kria Reviewer",
            auth_provider="reviewer",
            onboarding_status=source.onboarding_status,
        )
        db.add(reviewer)
        await db.flush()
        created_user = True
    else:
        # Mirror the source user's onboarding_status so a re-seed against a
        # freshly onboarded source account still unlocks the same UI state.
        reviewer.onboarding_status = source.onboarding_status

    already_seeded = {
        row[0]
        for row in (
            await db.execute(
                select(Job.assembly_plan["reviewer_seed_source_job_id"].astext).where(
                    Job.user_id == reviewer.id
                )
            )
        ).all()
        if row[0] is not None
    }

    source_jobs = (
        (
            await db.execute(
                select(Job)
                .where(Job.user_id == source.id, Job.status.in_(PLAN_ITEM_JOB_READY))
                .order_by(Job.created_at.desc())
                .limit(body.limit)
            )
        )
        .scalars()
        .all()
    )

    cloned: list[str] = []
    skipped: list[str] = []
    for source_job in source_jobs:
        if str(source_job.id) in already_seeded:
            skipped.append(str(source_job.id))
            continue
        new_job = _clone_job(source_job, reviewer)
        db.add(new_job)
        cloned.append(str(new_job.id))

    await db.commit()
    log.info(
        "admin_reviewer.seed.complete",
        reviewer_user_id=str(reviewer.id),
        created_user=created_user,
        cloned_count=len(cloned),
        skipped_count=len(skipped),
    )
    return SeedReviewerAccountResponse(
        reviewer_user_id=str(reviewer.id),
        created_user=created_user,
        cloned=cloned,
        skipped=skipped,
    )
