"""Durable, leased actual-output checks on the performance render worker.

The Job's reserved export is the outbox. Publishing to Celery is a hint: the
maintenance reconciler redelivers queued/abandoned entries without decoding any
media. Publication still uses the owner/source/recipe/upload fences in the
completion service. No worker holds database locks while decoding.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import structlog
from fastapi import HTTPException
from sqlalchemy import bindparam, func, select
from sqlalchemy.dialects.postgresql import JSONPATH
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.database import sync_session
from app.kria.device_render import DeviceExportCompleteBody
from app.models import Job
from app.services.device_render import (
    DEVICE_RENDER_FIELD,
    apply_device_failure_variant_update,
    device_record,
    has_accepted_artifact,
    mark_device_failed,
    save_device_record,
)
from app.services.job_storage_paths import project_media_reference_lock_key
from app.services.native_activity import activity_statement, record_native_activity
from app.services.pipeline_trace import pipeline_trace_for
from app.worker import celery_app

log = structlog.get_logger()
LEASE_SECONDS = 360  # strictly longer than the task's hard limit
MAX_CHECK_RUNS = 3  # infrastructure retries of the SAME bytes, not repair renders
MAX_QUEUE_SECONDS = 20 * 60
# A check that could not run (worker/queue/storage trouble) is requeued for the SAME
# uploaded bytes, and the phone keeps showing "Checking your video". After this long
# without one finished check the finished export publishes UNVERIFIED (checks are
# advisory; a finished video is never withheld because the checker is down). Only when
# even that publish cannot run does the creator see a failure, with honest copy.
RECHECK_WINDOW_SECONDS = 30 * 60
_UNAVAILABLE_DETAIL = "Kria couldn't check this video. Your approved edit is saved; try again."
PENDING_PATH = (
    '$._device_render_v1.* ? (@.status.phase == "syncing")'
    '.attempts.*.verification_task ? (@.state == "queued" || @.state == "running")'
)


def enqueue(job_id: str, user_id: str, body: dict) -> None:
    try:
        with celery_app.connection_for_write(connect_timeout=2) as connection:
            verify_device_export.apply_async(
                args=[job_id, user_id, body],
                queue="celery",
                connection=connection,
                retry=False,
            )
    except Exception:
        # The committed outbox remains authoritative, including after broker loss.
        log.warning("export_verification_publish_deferred", job_id=job_id)


def claim_valid(record: dict, attempt_id: str, token: str) -> bool:
    task = record.get("attempts", {}).get(attempt_id, {}).get("verification_task", {})
    return (
        task.get("state") == "running"
        and task.get("token") == token
        and datetime.fromisoformat(task["lease_until"]) > datetime.now(UTC)
    )


def _close_if_superseded(job, variant_id: str, record: dict, task: dict) -> bool:
    """Close the check of a record the variant has moved past (the reaper's fence).

    Requeueing it forever crowds genuine checks out of the reconciler, and giving up
    on it would label the NEWER attempt's variant and Job as failed.
    """
    from app.tasks.device_render_reaper import _superseded  # noqa: PLC0415

    if not _superseded(job, variant_id, record):
        return False
    task.pop("token", None)
    task.pop("lease_until", None)
    task.update(state="done", outcome="superseded")
    save_device_record(job, variant_id, record)
    return True


def _requeue_or_give_up(job, variant_id: str, record: dict, task: dict, now: datetime) -> bool:
    """Requeue a check that could not run; after the recheck window, publish unverified.

    Never re-renders and never asks the phone for anything: the reserved upload is
    the outbox, so the next delivery checks (or, past the window, publishes without
    checking) exactly the same bytes. Returns True when it requeued, False when it
    recorded the (honest) failure instead -- which happens only if the unverified
    publish itself already failed to run.
    """
    if _close_if_superseded(job, variant_id, record, task):
        # A newer approved attempt owns the variant: never fail it for a stale check.
        return False
    first = task.get("first_queued_at") or task.get("queued_at") or now.isoformat()
    spent = (now - datetime.fromisoformat(first)).total_seconds() >= RECHECK_WINDOW_SECONDS
    if not spent or not task.get("publish_unverified"):
        task.pop("token", None)
        task.pop("lease_until", None)
        task.update(
            state="queued",
            runs=0,
            queued_at=now.isoformat(),
            first_queued_at=first,
            requeues=int(task.get("requeues") or 0) + 1,
            # Past the window the next delivery publishes the finished export without
            # the checks that could not run.
            **({"publish_unverified": True} if spent else {}),
        )
        save_device_record(job, variant_id, record)
        return True
    task.update(state="done", outcome="infrastructure_failure")
    save_device_record(job, variant_id, record)
    mark_device_failed(
        job, variant_id, reason_code="verification_unavailable", detail=_UNAVAILABLE_DETAIL
    )
    if not has_accepted_artifact(job, variant_id):
        apply_device_failure_variant_update(
            job, variant_id, reason_code="verification_unavailable", detail=_UNAVAILABLE_DETAIL
        )
    return False


def _expire_queued(job_id: uuid.UUID, now: datetime) -> None:
    """A queue outage cannot leave the phone checking indefinitely."""
    with sync_session() as db:
        owner = db.execute(select(Job.user_id).where(Job.id == job_id)).scalar_one_or_none()
        if owner is None:
            return
        db.execute(select(func.pg_advisory_xact_lock(project_media_reference_lock_key(owner))))
        job = db.execute(select(Job).where(Job.id == job_id).with_for_update()).scalar_one_or_none()
        if job is None or job.user_id != owner or job.status == "cancelled":
            return
        requeued: list[tuple[str, dict]] = []
        for variant_id in (job.assembly_plan or {}).get(DEVICE_RENDER_FIELD, {}):
            record = device_record(job, variant_id)
            if record.get("status", {}).get("phase") != "syncing":
                continue
            for attempt_id, task in [
                (attempt_id, a.get("verification_task", {}))
                for attempt_id, a in record.get("attempts", {}).items()
            ]:
                queued = task.get("queued_at")
                if task.get("state") not in {"queued", "running"} or not queued:
                    continue
                # A valid running claim gets its entire hard-deadline lease.
                if (
                    task.get("state") == "running"
                    and datetime.fromisoformat(task["lease_until"]) > now
                ):
                    continue
                if (now - datetime.fromisoformat(queued)).total_seconds() <= MAX_QUEUE_SECONDS:
                    continue
                # A queue outage is not the phone's silence (never `timed_out`):
                # recheck the same bytes, and keep the phone on "Checking".
                if _requeue_or_give_up(job, variant_id, record, task, now):
                    requeued.append(
                        (
                            str(job.user_id),
                            {
                                "identity": record["status"]["request"]["identity"],
                                "attempt_id": attempt_id,
                            },
                        )
                    )
                statement = activity_statement(job, variant_id, kind="action_required")
                if statement is not None:
                    db.execute(statement)
                break
        db.commit()
    for user_id, body in requeued:
        enqueue(str(job_id), user_id, body)


async def _run(job_id: str, user_id: str, payload: dict) -> str:
    from app.routes.device_render import (
        _complete_device_export,
        _ContractRefusal,
        _DeviceConflict,
        _owned_job,
        _record,
        _refuse_publication,
    )

    # Celery deliveries have distinct event loops; never share asyncpg pools.
    engine = create_async_engine(settings.asyncpg_database_url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    body = DeviceExportCompleteBody.model_validate(payload)
    attempt_id = str(body.attempt_id)
    token = str(uuid.uuid4())
    try:
        async with sessions() as db:
            try:
                job = await _owned_job(db, uuid.UUID(user_id), uuid.UUID(job_id))
                try:
                    record, status = _record(job, body.identity)
                except _DeviceConflict as conflict:
                    if conflict.code == "superseded":
                        # The fence moved past this record: close its check instead
                        # of leaving it queued for the reconciler to redeliver.
                        stale = device_record(job, body.identity.variant_id)
                        stale_task = (
                            stale.get("attempts", {}).get(attempt_id, {}).get("verification_task")
                        )
                        if stale_task and stale_task.get("state") != "done":
                            stale_task.pop("token", None)
                            stale_task.pop("lease_until", None)
                            stale_task.update(state="done", outcome="superseded")
                            save_device_record(job, body.identity.variant_id, stale)
                            await db.commit()
                        return "superseded"
                    if isinstance(conflict, _ContractRefusal):
                        await _refuse_publication(db, job, conflict)
                        return "authority_refused"
                    raise
                task = record.get("attempts", {}).get(attempt_id, {}).get("verification_task")
                if not task or status.phase != "syncing" or task["state"] == "done":
                    return "obsolete"
                now = datetime.now(UTC)
                if task["state"] == "running" and datetime.fromisoformat(task["lease_until"]) > now:
                    return "claimed"
                if task["runs"] >= MAX_CHECK_RUNS:
                    requeued = _requeue_or_give_up(job, body.identity.variant_id, record, task, now)
                    await record_native_activity(db, job, body.identity.variant_id)
                    await db.commit()
                    # The maintenance reconciler redelivers a requeued check (its
                    # `queued_at` is due now), so a broker hiccup cannot strand it.
                    return "requeued" if requeued else "exhausted"
                task.update(
                    state="running",
                    token=token,
                    runs=task["runs"] + 1,
                    lease_until=(now + timedelta(seconds=LEASE_SECONDS)).isoformat(),
                )
                save_device_record(job, body.identity.variant_id, record)
                await db.commit()
                result = await _complete_device_export(
                    uuid.UUID(job_id),
                    body,
                    uuid.UUID(user_id),
                    db,
                    verification_token=token,
                    unverified=bool(task.get("publish_unverified")),
                )
                return result.status
            except HTTPException as exc:
                # Failed quality checks publish with a warning (advisory); only bytes that
                # differ from the reserved upload commit needs_attention in the publication
                # service. Missing upload / transient storage is retried under
                # this same finite lease, never by rendering another candidate.
                return f"refused:{exc.status_code}"
    finally:
        await engine.dispose()


@celery_app.task(
    name="tasks.verify_device_export",
    soft_time_limit=240,
    time_limit=300,
    max_retries=0,
    acks_late=True,
    reject_on_worker_lost=True,
)
def verify_device_export(job_id: str, user_id: str, payload: dict) -> str:
    with pipeline_trace_for(uuid.UUID(job_id)):
        return asyncio.run(_run(job_id, user_id, payload))


@celery_app.task(
    name="tasks.reconcile_export_verifications", soft_time_limit=60, time_limit=90, max_retries=0
)
def reconcile_export_verifications() -> dict[str, int]:
    now = datetime.now(UTC)
    with sync_session() as db:
        jobs = (
            db.execute(
                select(Job)
                .where(
                    Job.status != "cancelled",
                    func.jsonb_path_exists(
                        Job.assembly_plan, bindparam("pending_path", PENDING_PATH, type_=JSONPATH)
                    ),
                )
                .order_by(Job.updated_at)
                .limit(100)
            )
            .scalars()
            .all()
        )
        pending = []
        for job in jobs:
            for record in (job.assembly_plan or {}).get(DEVICE_RENDER_FIELD, {}).values():
                if (record.get("status") or {}).get("phase") != "syncing":
                    continue
                for attempt_id, attempt in record.get("attempts", {}).items():
                    task = attempt.get("verification_task", {})
                    due = task.get("lease_until") or task.get("queued_at")
                    if (
                        task.get("state") in {"queued", "running"}
                        and due
                        and datetime.fromisoformat(due) <= now
                    ):
                        pending.append(
                            (
                                str(job.id),
                                str(job.user_id),
                                {
                                    "identity": record["status"]["request"]["identity"],
                                    "attempt_id": attempt_id,
                                },
                            )
                        )
    for identifier in {job_id for job_id, _, _ in pending}:
        _expire_queued(uuid.UUID(identifier), now)
    for job_id, user_id, body in pending:
        enqueue(job_id, user_id, body)
    return {"redelivered": len(pending)}
