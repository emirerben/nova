"""Celery tasks backing account deletion (privacy-policy §9 / DELETE /me/account).

send_account_deletion_email  — fire-and-forget confirmation email (Resend), mirrors
                                the pattern in tasks/email.py.
purge_user_storage           — async GCS walk deleting everything under
                                users/{user_id}/ and every canonical/legacy output
                                prefix for each owned job, plus a best-effort delete
                                of each job's raw_storage_path (covers the legacy
                                dev-user/ and {user_id}/{job_id}/ upload shapes that
                                predate the users/ prefix — see infra/README.md).
sweep_job_storage_deletions  — Beat-driven dispatcher that recovers deletion
                                manifests after broker or worker loss.
purge_job_storage             — exact-key cleanup driven by a durable manifest;
                                failed keys remain persisted for backoff retries.

Split from the request path (routes/me.py) because a user with a large media
footprint could hold the request past FastAPI's own timeout — the DB rows are
deleted synchronously (cheap), the GCS bytes are swept here (potentially slow).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import structlog
from billiard.exceptions import SoftTimeLimitExceeded
from sqlalchemy import and_, delete, or_, select

from app.config import settings
from app.database import sync_session
from app.models import JobStorageDeletion
from app.services.job_storage_deletion import (
    cleanup_job_storage_manifest,
    parse_job_storage_manifest,
)
from app.services.job_storage_paths import JOB_OUTPUT_PREFIXES
from app.services.video_poster_cleanup import (
    jobs_with_video_poster_cleanup_receipts,
    reconcile_video_poster_cleanup_receipts,
)
from app.worker import celery_app

log = structlog.get_logger()

_JOB_STORAGE_DELETION_LEASE = timedelta(minutes=10)
_JOB_STORAGE_DELETION_RETRY_BASE_S = 60
_JOB_STORAGE_DELETION_RETRY_MAX_S = 3600
_JOB_STORAGE_DELETION_RETENTION = timedelta(days=30)
# Cleanup service: 2 receipts/job × at most 3 GCS calls × 3s × 2 jobs =
# 36s worst-case storage time, leaving headroom inside this task's 60s soft limit.
_VIDEO_POSTER_CLEANUP_SWEEP_LIMIT = 2


def cleanup_job_storage_paths(object_paths: list[str]) -> tuple[int, list[str]]:
    """Best-effort delete of exact object keys, returning only failed keys.

    The caller has already validated that these keys belong to one deleted Job.
    Keeping the helper synchronous makes it usable by Celery and the narrow
    post-commit fallback in the DELETE /me/jobs route.
    """
    from app.storage import delete_object_best_effort  # noqa: PLC0415

    deleted = 0
    failed: list[str] = []
    for path in dict.fromkeys(object_paths):
        if not isinstance(path, str) or not path.strip():
            continue
        if delete_object_best_effort(path):
            deleted += 1
        else:
            failed.append(path)
    return deleted, failed


@celery_app.task(name="tasks.send_account_deletion_email", max_retries=0)
def send_account_deletion_email(email: str, confirm_token: str) -> None:
    """Email the one-time confirmation code for POST /me/account/delete-confirm.

    Uses Resend's HTTP API directly, matching tasks/email.py's waitlist pattern.
    If RESEND_API_KEY is not configured, logs a warning and returns — deletion is
    still blocked without the code (fails closed, not open).
    """
    api_key = getattr(settings, "resend_api_key", "")
    if not api_key:
        log.warning("resend_api_key_not_configured", email=email, task="account_deletion")
        return

    import httpx

    try:
        response = httpx.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": "Kria <hello@usekria.com>",
                "to": [email],
                "subject": "Confirm your Kria account deletion",
                "html": _build_email_html(confirm_token),
            },
            timeout=10.0,
        )
        response.raise_for_status()
        log.info("account_deletion_email_sent", email=email, resend_id=response.json().get("id"))
    except httpx.HTTPStatusError as exc:
        log.error(
            "account_deletion_email_failed",
            email=email,
            status_code=exc.response.status_code,
            detail=exc.response.text[:500],
        )
    except Exception as exc:  # noqa: BLE001 — fire-and-forget, never retried
        log.error("account_deletion_email_error", email=email, error=str(exc))


def _build_email_html(confirm_token: str) -> str:
    return f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
                max-width: 480px; margin: 0 auto; padding: 40px 20px;">
        <h1 style="font-size: 22px; margin-bottom: 16px;">Confirm account deletion</h1>
        <p style="color: #555; line-height: 1.6;">
            Someone (hopefully you) asked to permanently delete your Kria account and
            all associated data — your persona, content plans, uploaded footage, and
            rendered videos. This cannot be undone.
        </p>
        <p style="color: #555; line-height: 1.6;">
            To confirm, paste this code back into the account deletion screen. It
            expires in 1 hour.
        </p>
        <p style="font-family: monospace; font-size: 13px; word-break: break-all;
                   background: #f4f4f5; padding: 12px; border-radius: 8px;">
            {confirm_token}
        </p>
        <p style="color: #999; font-size: 12px;">
            If you didn't request this, ignore this email — your account is unaffected.
        </p>
    </div>
    """


@celery_app.task(
    name="tasks.purge_user_storage",
    bind=True,
    autoretry_for=(),
    max_retries=0,
    soft_time_limit=1500,
    time_limit=1740,
)
def purge_user_storage(
    self, user_id: str, job_ids: list[str], raw_storage_paths: list[str]
) -> dict:
    """Delete every GCS object belonging to a deleted user.

    Runs AFTER the DB rows are already gone (routes/me.py commits the DB delete
    before dispatching this), so the ids passed in are the only record of what to
    clean up — they can't be re-derived from the database once this runs.

    Sweeps three target sets:
      - users/{user_id}/            (plan clips, plan-pool, seed batches — never
                                       lifecycle-swept, see infra/gcs-lifecycle.json)
      - every JOB_OUTPUT_PREFIXES entry plus the legacy
        {user_id}/{job_id}/ namespace for each captured owned job id
      - each job's raw_storage_path directly (covers legacy dev-user/ and
        {user_id}/{job_id}/ shapes that predate the users/ prefix)

    Best-effort throughout — a partial failure costs orphaned storage, never
    retried automatically (retrying a delete is safe, but a stuck GCS outage
    shouldn't hold a Celery worker slot). Logs counts for observability.
    """
    from app.storage import delete_object_best_effort, delete_prefix_best_effort  # noqa: PLC0415

    canonical_user_id = str(uuid.UUID(user_id))
    user_deleted = delete_prefix_best_effort(f"users/{canonical_user_id}/")

    job_deleted = 0
    valid_job_ids: list[str] = []
    for raw_job_id in job_ids:
        try:
            job_id = str(uuid.UUID(raw_job_id))
        except (TypeError, ValueError, AttributeError):
            log.error(
                "purge_user_storage_invalid_job_id",
                user_id=canonical_user_id,
                job_id=raw_job_id,
            )
            continue
        valid_job_ids.append(job_id)
        for prefix_template in JOB_OUTPUT_PREFIXES:
            job_deleted += delete_prefix_best_effort(prefix_template.format(job_id=job_id))
        job_deleted += delete_prefix_best_effort(f"{canonical_user_id}/{job_id}/")

    raw_deleted = 0
    for path in raw_storage_paths:
        if path and delete_object_best_effort(path):
            raw_deleted += 1

    log.info(
        "purge_user_storage_done",
        user_id=canonical_user_id,
        job_count=len(valid_job_ids),
        user_prefix_objects_deleted=user_deleted,
        job_prefix_objects_deleted=job_deleted,
        raw_paths_deleted=raw_deleted,
    )
    return {
        "user_prefix_objects_deleted": user_deleted,
        "job_prefix_objects_deleted": job_deleted,
        "raw_paths_deleted": raw_deleted,
    }


def _claim_job_storage_deletion(
    outbox_id: str,
) -> tuple[uuid.UUID, object, int] | None:
    """Claim one due manifest, recovering leases abandoned by dead workers."""
    now = datetime.now(UTC)
    with sync_session() as db:
        deletion = db.execute(
            select(JobStorageDeletion).where(JobStorageDeletion.id == outbox_id).with_for_update()
        ).scalar_one_or_none()
        if deletion is None or deletion.status == "completed":
            return None
        if deletion.status == "processing":
            if deletion.lease_until is not None and deletion.lease_until > now:
                return None
        elif deletion.next_attempt_at is not None and deletion.next_attempt_at > now:
            return None

        deletion.status = "processing"
        deletion.attempts += 1
        deletion.lease_until = now + _JOB_STORAGE_DELETION_LEASE
        db.commit()
        payload = deletion.object_paths
        if isinstance(payload, list):
            payload = list(payload)
        elif isinstance(payload, dict):
            payload = dict(payload)
        return deletion.job_id, payload, deletion.attempts


def _finish_job_storage_deletion(
    outbox_id: str,
    *,
    expected_attempt: int,
    remaining_payload: object,
    completed: bool,
    error: str | None = None,
    retry_not_before: datetime | None = None,
) -> bool:
    """Finish only the exact lease claim that produced ``remaining_payload``.

    A storage sweep can outlive its lease.  In that case another worker may
    reclaim the row, or account erasure may merge additional cleanup debt and
    reset it to pending.  The old worker's result was computed from a stale
    manifest and must never overwrite either newer state.
    """
    now = datetime.now(UTC)
    with sync_session() as db:
        deletion = db.execute(
            select(JobStorageDeletion).where(JobStorageDeletion.id == outbox_id).with_for_update()
        ).scalar_one_or_none()
        if (
            deletion is None
            or deletion.status != "processing"
            or deletion.attempts != expected_attempt
        ):
            return False

        deletion.lease_until = None
        if not completed:
            deletion.status = "pending"
            deletion.object_paths = remaining_payload
            retry_delay = min(
                _JOB_STORAGE_DELETION_RETRY_BASE_S * (2 ** min(max(deletion.attempts - 1, 0), 6)),
                _JOB_STORAGE_DELETION_RETRY_MAX_S,
            )
            retry_at = now + timedelta(seconds=retry_delay)
            if retry_not_before is not None:
                retry_at = max(retry_at, retry_not_before.astimezone(UTC))
            deletion.next_attempt_at = retry_at
            deletion.last_error = error
            deletion.completed_at = None
        else:
            deletion.status = "completed"
            deletion.object_paths = remaining_payload
            deletion.next_attempt_at = None
            deletion.last_error = None
            deletion.completed_at = now
        db.commit()
        return True


@celery_app.task(
    name="tasks.purge_job_storage",
    autoretry_for=(),
    max_retries=0,
    soft_time_limit=300,
    time_limit=360,
)
def purge_job_storage(outbox_id: str) -> dict:
    """Process a durable deletion manifest and retain failures for retry."""
    claimed = _claim_job_storage_deletion(outbox_id)
    if claimed is None:
        return {"status": "skipped", "deleted": 0, "failed": 0}

    job_id, payload, attempt = claimed
    error: str | None = None
    prefixes_verified = 0
    prefixes_pending = 0
    retry_not_before: datetime | None = None
    if isinstance(payload, list):
        try:
            deleted, failed = cleanup_job_storage_paths(payload)
        except SoftTimeLimitExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 — lease recovery handles outages
            deleted = 0
            failed = payload
            error = type(exc).__name__
        remaining_payload: object = failed
        completed = not failed
        failed_count = len(failed)
        if failed and error is None:
            error = f"{failed_count} storage objects could not be deleted"
    else:
        now = datetime.now(UTC)
        try:
            manifest = parse_job_storage_manifest(payload, job_id=job_id)
            cleanup = cleanup_job_storage_manifest(manifest, now=now)
        except SoftTimeLimitExceeded:
            # Do not overwrite the committed manifest when Celery's execution
            # budget expires; the processing lease makes it recoverable.
            raise
        except Exception as exc:  # noqa: BLE001 — malformed debt is retained
            deleted = 0
            failed_count = 1
            completed = False
            remaining_payload = payload
            error = type(exc).__name__
        else:
            deleted = cleanup.exact_deleted
            failed_count = len(cleanup.remaining.exact_paths)
            prefixes_verified = cleanup.prefixes_verified
            prefixes_pending = len(cleanup.remaining.prefixes)
            completed = cleanup.complete
            remaining_payload = cleanup.remaining.to_payload()
            if cleanup.status == "unavailable":
                error = "storage_unavailable"
            elif not completed and (
                cleanup.remaining.exact_paths
                or any(entry.not_before <= now for entry in cleanup.remaining.prefixes)
            ):
                error = "storage_cleanup_incomplete"
            if (
                not cleanup.remaining.exact_paths
                and cleanup.remaining.prefixes
                and all(entry.not_before > now for entry in cleanup.remaining.prefixes)
            ):
                retry_not_before = min(entry.not_before for entry in cleanup.remaining.prefixes)

    finish_accepted = _finish_job_storage_deletion(
        outbox_id,
        expected_attempt=attempt,
        remaining_payload=remaining_payload,
        completed=completed,
        error=error,
        retry_not_before=retry_not_before,
    )
    if not finish_accepted:
        log.info(
            "purge_job_storage_result_superseded",
            outbox_id=outbox_id,
            attempt=attempt,
        )
        result = {
            "status": "superseded",
            "deleted": deleted,
            "failed": failed_count,
        }
        if isinstance(payload, dict):
            result.update(
                prefixes_verified=prefixes_verified,
                prefixes_pending=prefixes_pending,
            )
        return result
    if not completed:
        log.warning(
            "purge_job_storage_pending_retry",
            outbox_id=outbox_id,
            deleted=deleted,
            failed=failed_count,
            prefixes_pending=prefixes_pending,
            attempt=attempt,
        )
        result = {"status": "pending", "deleted": deleted, "failed": failed_count}
        if isinstance(payload, dict):
            result.update(
                prefixes_verified=prefixes_verified,
                prefixes_pending=prefixes_pending,
            )
        return result

    log.info(
        "purge_job_storage_done",
        outbox_id=outbox_id,
        deleted=deleted,
        prefixes_verified=prefixes_verified,
    )
    result = {"status": "completed", "deleted": deleted, "failed": 0}
    if isinstance(payload, dict):
        result.update(prefixes_verified=prefixes_verified, prefixes_pending=0)
    return result


@celery_app.task(
    name="tasks.sweep_job_storage_deletions",
    autoretry_for=(),
    max_retries=0,
    soft_time_limit=60,
    time_limit=90,
)
def sweep_job_storage_deletions(limit: int = 100) -> dict:
    """Recover durable storage manifests and displaced-poster receipts."""
    now = datetime.now(UTC)
    due = or_(
        and_(
            JobStorageDeletion.status == "pending",
            or_(
                JobStorageDeletion.next_attempt_at.is_(None),
                JobStorageDeletion.next_attempt_at <= now,
            ),
        ),
        and_(
            JobStorageDeletion.status == "processing",
            or_(
                JobStorageDeletion.lease_until.is_(None),
                JobStorageDeletion.lease_until <= now,
            ),
        ),
    )
    with sync_session() as db:
        outbox_ids = list(
            db.execute(
                select(JobStorageDeletion.id)
                .where(due)
                .order_by(JobStorageDeletion.created_at)
                .limit(limit)
            ).scalars()
        )
        pruned = (
            db.execute(
                delete(JobStorageDeletion).where(
                    JobStorageDeletion.status == "completed",
                    JobStorageDeletion.completed_at < now - _JOB_STORAGE_DELETION_RETENTION,
                )
            ).rowcount
            or 0
        )
        poster_job_ids = jobs_with_video_poster_cleanup_receipts(
            db,
            limit=min(max(limit, 0), _VIDEO_POSTER_CLEANUP_SWEEP_LIMIT),
        )
        db.commit()

    dispatched = 0
    dispatch_failed = 0
    for outbox_id in outbox_ids:
        try:
            purge_job_storage.apply_async(args=[str(outbox_id)])
            dispatched += 1
        except Exception as exc:  # noqa: BLE001 — next Beat sweep retries
            dispatch_failed += 1
            log.error(
                "purge_job_storage_dispatch_failed",
                outbox_id=str(outbox_id),
                error=str(exc),
            )

    poster_receipts_scanned = 0
    poster_receipts_deleted = 0
    poster_receipts_pending = 0
    poster_receipt_failures = 0
    for job_id in poster_job_ids:
        try:
            cleanup_result = reconcile_video_poster_cleanup_receipts(job_id)
        except SoftTimeLimitExceeded:
            # Let Celery terminate the sweep immediately. Treating the soft
            # deadline as an ordinary per-job failure would start another GCS
            # reconciliation after the task's execution budget was exhausted.
            raise
        except Exception as exc:  # noqa: BLE001 — next Beat sweep retries
            poster_receipt_failures += 1
            log.error(
                "video_poster_cleanup_sweep_failed",
                job_id=str(job_id),
                error=str(exc),
            )
            continue
        poster_receipts_scanned += cleanup_result.receipts_seen
        poster_receipts_deleted += cleanup_result.deleted
        poster_receipts_pending += cleanup_result.retained
        poster_receipt_failures += cleanup_result.failures

    return {
        "dispatched": dispatched,
        "dispatch_failed": dispatch_failed,
        "pruned": pruned,
        "poster_receipts_scanned": poster_receipts_scanned,
        "poster_receipts_deleted": poster_receipts_deleted,
        "poster_receipts_pending": poster_receipts_pending,
        "poster_receipt_failures": poster_receipt_failures,
    }
