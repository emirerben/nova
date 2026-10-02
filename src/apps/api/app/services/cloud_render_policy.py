"""Fail-closed policy for the retired server-side video render path.

Device Jobs still run on the worker because their first orchestrator pass
compiles an immutable recipe for the phone.  Cloud Jobs are terminalized before
broker publication (and again at task entry for messages already on Redis) when
the execution kill switch is off.  Ready outputs are never rewritten.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.kria.media_sources import is_device_render_job
from app.models import Job

log = structlog.get_logger()

CLOUD_RENDER_DISABLED_REASON = "cloud_render_disabled"
CLOUD_RENDER_DISABLED_DETAIL = (
    "Server video rendering is disabled. Update Kria to render this project on your iPhone."
)

# Only in-flight rows may be terminalized.  A ready/partially-ready Job can be
# the parent of a rerender request; preserving it keeps its last-good output
# playable even when the new render attempt is rejected.
_ACTIVE_CLOUD_STATUSES = frozenset(
    {
        "queued",
        "processing",
        "matching",
        "rendering",
        "posting",
    }
)


def cloud_render_is_blocked(job: object) -> bool:
    """Return True only for a cloud-destination Job while execution is disabled."""

    return not settings.cloud_render_execution_enabled and not is_device_render_job(job)


def cloud_render_mutation_block_reason(
    job: object,
    *,
    variant: object | None = None,
) -> str | None:
    """Why a new render-affecting mutation must be rejected, if at all.

    Admission and execution are separate rollout fences.  During iOS-only
    admission, an existing cloud project stays readable/playable but cannot
    mint a replacement render.  Once the executor kill switch is off, the same
    pre-mutation refusal protects old outputs even if a caller bypasses the
    HTTP admission middleware.  Exact variant intent wins when available so a
    mixed/legacy Job can never make a cloud variant look device-bound.
    """

    if isinstance(variant, dict):
        device_bound = variant.get("render_destination") in {"device", "phone"}
    else:
        device_bound = is_device_render_job(job)
    if device_bound:
        return None
    if settings.ios_device_only_mode:
        return "device_render_unsupported"
    if not settings.cloud_render_execution_enabled:
        return CLOUD_RENDER_DISABLED_REASON
    return None


def _terminalize_if_active(job: Any) -> bool:
    if str(getattr(job, "status", "")) not in _ACTIVE_CLOUD_STATUSES:
        return False
    job.status = "processing_failed"
    job.failure_reason = CLOUD_RENDER_DISABLED_REASON
    job.error_detail = CLOUD_RENDER_DISABLED_DETAIL
    return True


async def block_cloud_render_before_publish(
    db: AsyncSession,
    job_id: uuid.UUID,
    *,
    task_name: str,
) -> bool:
    """Reject a cloud orchestrator before publication, preserving device Jobs.

    Returns True when publication must be skipped.  The caller intentionally
    treats a ready Job as blocked without rewriting it: the old output remains
    authoritative and no expensive replacement work reaches Redis.
    """

    if settings.cloud_render_execution_enabled:
        return False
    job = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
    if job is None or not cloud_render_is_blocked(job):
        return False
    terminalized = _terminalize_if_active(job)
    if terminalized:
        await db.commit()
    log.warning(
        "cloud_render_publish_blocked",
        job_id=str(job_id),
        task_name=task_name,
        status=str(getattr(job, "status", "unknown")),
        terminalized=terminalized,
    )
    return True


def block_cloud_render_before_publish_sync(
    job_id: uuid.UUID,
    *,
    task_name: str,
) -> bool:
    """Synchronous twin used by Celery-side orchestrator dispatch helpers."""

    if settings.cloud_render_execution_enabled:
        return False
    from app.database import sync_session  # noqa: PLC0415

    with sync_session() as db:
        job = db.execute(select(Job).where(Job.id == job_id)).scalar_one_or_none()
        if job is None or not cloud_render_is_blocked(job):
            return False
        terminalized = _terminalize_if_active(job)
        if terminalized:
            db.commit()
        log.warning(
            "cloud_render_publish_blocked",
            job_id=str(job_id),
            task_name=task_name,
            status=str(getattr(job, "status", "unknown")),
            terminalized=terminalized,
        )
        return True


def block_cloud_render_task(job_id: str, *, task_name: str) -> bool:
    """Defense-in-depth task-entry guard for messages queued before cutover."""

    if settings.cloud_render_execution_enabled:
        return False
    try:
        job_uuid = uuid.UUID(str(job_id))
    except (TypeError, ValueError, AttributeError):
        return False
    return block_cloud_render_before_publish_sync(job_uuid, task_name=task_name)
