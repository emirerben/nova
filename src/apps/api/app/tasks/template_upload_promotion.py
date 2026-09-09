"""Recover template uploads whose API process died during promotion/dispatch."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import and_, or_, select

from app.database import sync_session
from app.models import Job, VideoTemplate
from app.services.job_dispatch import claim_and_enqueue_orchestrator_sync
from app.services.template_upload_promotion import (
    TEMPLATE_UPLOAD_PROMOTION_FIELD,
    record_template_upload_promotion_failure,
    resume_template_upload_promotion,
)
from app.worker import celery_app

log = structlog.get_logger()

_RECOVERY_GRACE = timedelta(seconds=30)
_RECOVERY_BATCH = 1


def _candidate_ids(*, now: datetime) -> list[str]:
    cutoff = now - _RECOVERY_GRACE
    with sync_session() as db:
        rows = db.execute(
            select(Job.id)
            .where(
                Job.job_type == "template",
                Job.updated_at <= cutoff,
                or_(
                    and_(
                        Job.status == "importing",
                        Job.assembly_plan.op("?")(TEMPLATE_UPLOAD_PROMOTION_FIELD),
                    ),
                    and_(Job.status == "queued", Job.celery_task_id.is_(None)),
                ),
            )
            .order_by(Job.updated_at, Job.id)
            .limit(_RECOVERY_BATCH)
        ).scalars()
        return [str(job_id) for job_id in rows]


def _dispatch_if_ready(job_id: str) -> bool:
    with sync_session() as db:
        row = db.execute(
            select(Job, VideoTemplate.recipe_cached)
            .outerjoin(VideoTemplate, VideoTemplate.id == Job.template_id)
            .where(Job.id == job_id)
        ).one_or_none()
        if row is None:
            return False
        job, recipe_cached = row
        if job.status != "queued" or job.celery_task_id is not None:
            return False
        template_kind = (recipe_cached or {}).get("template_kind", "multiple_videos")

    from app.tasks.template_orchestrate import (  # noqa: PLC0415
        orchestrate_single_video_job,
        orchestrate_template_job,
    )

    task = (
        orchestrate_single_video_job
        if template_kind == "single_video"
        else orchestrate_template_job
    )
    return claim_and_enqueue_orchestrator_sync(task, job_id)


def reconcile_template_upload_promotions(*, now: datetime | None = None) -> int:
    """Resume one stale journal, then publish its render if still unowned."""

    recovered = 0
    for job_id in _candidate_ids(now=now or datetime.now(UTC)):
        try:
            result = resume_template_upload_promotion(job_id)
            if result.state in {"promoted", "ready"} and _dispatch_if_ready(job_id):
                recovered += 1
        except Exception as exc:  # noqa: BLE001 — the next Beat pass retries
            record_template_upload_promotion_failure(
                job_id,
                error_type=type(exc).__name__,
                now=now,
            )
            log.warning(
                "template_upload_promotion_reconcile_failed",
                job_id=job_id,
                error_type=type(exc).__name__,
            )
    return recovered


@celery_app.task(
    name="tasks.reconcile_template_upload_promotions",
    bind=True,
    autoretry_for=(),
    max_retries=0,
    soft_time_limit=60,
    time_limit=90,
)
def reconcile_template_upload_promotions_task(self) -> int:  # noqa: ARG001
    return reconcile_template_upload_promotions()
