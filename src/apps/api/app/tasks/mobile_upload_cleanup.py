"""Bounded cleanup for native temporary-media reservations."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import and_, func, or_, select

from app.database import sync_session
from app.models import TemporaryMediaUpload
from app.services.job_storage_paths import project_media_reference_lock_key
from app.storage import delete_object_best_effort
from app.worker import celery_app

log = structlog.get_logger()

_BATCH_SIZE = 50
_CLAIM_TIMEOUT = timedelta(minutes=15)


def cleanup_expired_temporary_uploads(
    *,
    now: datetime | None = None,
    limit: int = _BATCH_SIZE,
) -> int:
    """Claim, delete, and receipt one bounded page of expired uploads.

    GCS lifecycle remains a final backstop. The database receipt lets cancel
    requests and transient storage failures converge without trusting a device
    process to remain alive.
    """

    current = now or datetime.now(UTC)
    stale_claim = current - _CLAIM_TIMEOUT
    bounded = min(max(int(limit), 0), _BATCH_SIZE)
    if bounded == 0:
        return 0

    eligible = or_(
        and_(
            TemporaryMediaUpload.status == "reserved",
            TemporaryMediaUpload.retention_expires_at <= current,
        ),
        and_(
            TemporaryMediaUpload.status == "cleanup_pending",
            TemporaryMediaUpload.retention_expires_at <= current,
            or_(
                TemporaryMediaUpload.cleanup_claimed_at.is_(None),
                TemporaryMediaUpload.cleanup_claimed_at <= stale_claim,
            ),
        ),
    )
    claimed: list[tuple[uuid.UUID, str]] = []
    with sync_session() as db:
        # Discover without row locks, then acquire owner locks in deterministic
        # order before locking and rechecking receipts. Job attachment uses the
        # same owner lock, avoiding both delete-after-attach and lock inversion.
        candidates = db.execute(
            select(TemporaryMediaUpload.id, TemporaryMediaUpload.user_id)
            .where(TemporaryMediaUpload.deleted_at.is_(None), eligible)
            .order_by(
                TemporaryMediaUpload.retention_expires_at,
                TemporaryMediaUpload.user_id,
                TemporaryMediaUpload.id,
            )
            .limit(bounded)
        ).all()
        candidate_ids = [row.id for row in candidates]
        for owner_id in sorted({row.user_id for row in candidates}, key=str):
            db.execute(
                select(func.pg_advisory_xact_lock(project_media_reference_lock_key(owner_id)))
            )
        rows = (
            db.execute(
                select(TemporaryMediaUpload)
                .where(
                    TemporaryMediaUpload.id.in_(candidate_ids),
                    TemporaryMediaUpload.deleted_at.is_(None),
                    eligible,
                )
                .order_by(
                    TemporaryMediaUpload.retention_expires_at,
                    TemporaryMediaUpload.user_id,
                    TemporaryMediaUpload.id,
                )
                .with_for_update(skip_locked=True)
            )
            .scalars()
            .all()
        )
        for row in rows:
            row.status = "cleanup_pending"
            row.cleanup_claimed_at = current
            row.delete_attempts = int(row.delete_attempts or 0) + 1
            claimed.append((row.id, row.object_path))
        db.commit()

    deleted_count = 0
    for reservation_id, object_path in claimed:
        deleted = delete_object_best_effort(object_path)
        with sync_session() as db:
            row = db.get(TemporaryMediaUpload, reservation_id, with_for_update=True)
            if row is None or row.object_path != object_path or row.deleted_at is not None:
                continue
            if deleted:
                row.status = "deleted"
                row.deleted_at = datetime.now(UTC)
                row.last_error = None
                deleted_count += 1
            else:
                row.last_error = "storage_unavailable"
            db.commit()

    if claimed:
        log.info(
            "temporary_media_uploads_cleaned",
            claimed=len(claimed),
            deleted=deleted_count,
        )
    return deleted_count


@celery_app.task(
    name="tasks.cleanup_temporary_media_uploads",
    soft_time_limit=50,
    time_limit=60,
)
def cleanup_temporary_media_uploads() -> int:
    return cleanup_expired_temporary_uploads()
