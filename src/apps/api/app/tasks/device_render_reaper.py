"""Stale device-render reaper (KRI-114 P0-3, sibling to reaper.py/build_task_reaper.py).

A phone-pinned device recipe (app/services/device_render.py) has no OS-level
completion signal: if the app crashes, gets deleted, or the push
notification is never seen, the job sits `awaiting_device` (or `syncing`,
mid-upload) forever. `POST /device-render/failures` (P0-1) covers the
client-driven half of this — a phone that IS still running can now tell the
server it gave up. This sweep is the backstop for the other failure mode: no
signal from the client at all.

Staleness is judged per-record (not per-job) against the freshest timestamp
we have for it: `last_polled_at` if the client has ever polled
`GET /device-render` (throttled to once per 60s — see
app/routes/device_render.py), else `pinned_at` (stamped when the recipe was
first pinned — app/services/device_render.py::pin_device_request), else
`job.updated_at` for the rare record that predates both fields.

Two-phase discovery/mutation, matching reaper.py's discipline: read a
bounded page of candidate job IDs with no lock, then independently lock,
revalidate, and commit each one — so one job's mutation never holds a lock
while iterating the whole page.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import select

from app.config import settings
from app.database import sync_session
from app.models import Job
from app.services.device_render import (
    DEVICE_RENDER_FIELD,
    apply_device_failure_variant_update,
    device_record,
    mark_device_failed,
    save_device_record,
)
from app.worker import celery_app

log = structlog.get_logger()

# Phases a stale record may be reaped from — mirrors _FAILABLE_PHASES in
# app/services/device_render.py (mark_device_failed enforces the same gate).
_STALE_PHASES = ("awaiting_device", "syncing")

# This is a pilot-cohort feature; jobs genuinely stuck `awaiting_device`
# should be rare. A generous cap keeps one sweep tick bounded if that
# assumption ever regresses.
_BATCH_LIMIT = 200

_STALE_DETAIL = "No delivery from your iPhone in the last 24 hours. Open the project to retry."


def _record_freshness(record: dict[str, Any], job_updated_at: datetime) -> datetime:
    """The most recent signal we have that a record's device is still alive."""
    for key in ("last_polled_at", "pinned_at"):
        raw = record.get(key)
        if isinstance(raw, str):
            try:
                return datetime.fromisoformat(raw)
            except ValueError:
                continue
    return job_updated_at


def _reap_job(job_id: Any, *, threshold_s: int, now: datetime) -> int:
    """Lock, revalidate, and reap stale records on one job. Returns count reaped."""
    reaped = 0
    with sync_session() as db:
        job = (
            db.execute(
                select(Job)
                .where(Job.id == job_id, Job.status == "awaiting_device")
                .with_for_update()
            )
        ).scalar_one_or_none()
        if job is None:
            return 0
        records = (job.assembly_plan or {}).get(DEVICE_RENDER_FIELD) or {}
        if not isinstance(records, dict):
            return 0
        for variant_id, record in records.items():
            if not isinstance(record, dict) or record.get("reaped_at"):
                continue
            status = record.get("status") or {}
            if not isinstance(status, dict) or status.get("phase") not in _STALE_PHASES:
                continue
            freshness = _record_freshness(record, job.updated_at)
            if (now - freshness) < timedelta(seconds=threshold_s):
                continue
            try:
                mark_device_failed(job, variant_id, reason_code="unknown", detail=_STALE_DETAIL)
            except ValueError:
                # Phase moved under us (e.g. a client-driven failure report or a
                # publish raced this sweep) — leave it alone, nothing to reap.
                continue
            # Mark the record so a future tick never re-reaps it.
            fresh = device_record(job, variant_id)
            fresh["reaped_at"] = now.isoformat()
            save_device_record(job, variant_id, fresh)
            apply_device_failure_variant_update(
                job, variant_id, reason_code="device_render_stale", detail=_STALE_DETAIL
            )
            reaped += 1
        if reaped:
            db.commit()
    return reaped


@celery_app.task(
    name="tasks.reap_stale_device_renders",
    bind=True,
    max_retries=0,
    soft_time_limit=120,
    time_limit=180,
)
def reap_stale_device_renders(self, *, stale_after_s: int | None = None) -> dict[str, int]:
    """Flip abandoned device recipes to needs_attention. Returns a summary count dict.

    `stale_after_s` overrides `settings.device_render_stale_after_s` — used by
    tests to reap without waiting a full day.
    """
    threshold_s = (
        stale_after_s if stale_after_s is not None else settings.device_render_stale_after_s
    )
    now = datetime.now(UTC)
    with sync_session() as db:
        job_ids = (
            db.execute(
                select(Job.id)
                .where(Job.status == "awaiting_device")
                .order_by(Job.updated_at.asc())
                .limit(_BATCH_LIMIT)
            )
            .scalars()
            .all()
        )
    reaped_jobs = 0
    reaped_records = 0
    for job_id in job_ids:
        count = _reap_job(job_id, threshold_s=threshold_s, now=now)
        if count:
            reaped_jobs += 1
            reaped_records += count
    if reaped_records:
        log.info(
            "device_render_reaper_swept",
            reaped_jobs=reaped_jobs,
            reaped_records=reaped_records,
            scanned_jobs=len(job_ids),
            threshold_s=threshold_s,
        )
    return {
        "reaped_jobs": reaped_jobs,
        "reaped_records": reaped_records,
        "scanned_jobs": len(job_ids),
    }
