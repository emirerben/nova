"""Retry Apple credential revocations persisted by account erasure."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import and_, or_, select

from app.database import sync_session
from app.models import AppleRevocationOutbox
from app.services.apple_account_revocation import revoke_refresh_token
from app.worker import celery_app

log = structlog.get_logger()
_LEASE = timedelta(minutes=5)


def _claim(raw_id: str) -> tuple[bytes, str, int] | None:
    try:
        outbox_id = uuid.UUID(raw_id)
    except (TypeError, ValueError):
        return None
    now = datetime.now(UTC)
    with sync_session() as db:
        row = db.execute(
            select(AppleRevocationOutbox)
            .where(AppleRevocationOutbox.id == outbox_id)
            .with_for_update()
        ).scalar_one_or_none()
        if (
            row is None
            or (row.next_attempt_at and row.next_attempt_at > now)
            or (row.lease_until and row.lease_until > now)
        ):
            return None
        row.attempts += 1
        row.lease_until = now + _LEASE
        db.commit()
        return row.encrypted_refresh_token, row.client_id, row.attempts


@celery_app.task(
    name="tasks.revoke_apple_credential",
    autoretry_for=(),
    max_retries=0,
    soft_time_limit=30,
    time_limit=60,
)  # noqa: E501
def revoke_apple_credential(outbox_id: str) -> dict:
    claimed = _claim(outbox_id)
    if claimed is None:
        return {"status": "skipped"}
    encrypted, client_id, attempt = claimed
    success = revoke_refresh_token(encrypted, client_id)
    now = datetime.now(UTC)
    with sync_session() as db:
        row = db.execute(
            select(AppleRevocationOutbox)
            .where(AppleRevocationOutbox.id == uuid.UUID(outbox_id))
            .with_for_update()
        ).scalar_one_or_none()
        if row is None or row.attempts != attempt:
            return {"status": "superseded"}
        if success:
            db.delete(row)
            db.commit()
            return {"status": "revoked"}
        row.lease_until = None
        row.next_attempt_at = now + timedelta(seconds=min(60 * (2 ** min(attempt - 1, 6)), 3600))
        row.last_error_code = "apple_revoke_failed"
        db.commit()
    return {"status": "pending"}


@celery_app.task(
    name="tasks.sweep_apple_revocations",
    autoretry_for=(),
    max_retries=0,
    soft_time_limit=30,
    time_limit=60,
)  # noqa: E501
def sweep_apple_revocations(limit: int = 100) -> dict:
    now = datetime.now(UTC)
    due = and_(
        or_(
            AppleRevocationOutbox.next_attempt_at.is_(None),
            AppleRevocationOutbox.next_attempt_at <= now,
        ),
        or_(
            AppleRevocationOutbox.lease_until.is_(None),
            AppleRevocationOutbox.lease_until <= now,
        ),
    )  # noqa: E501
    with sync_session() as db:
        ids = list(
            db.execute(
                select(AppleRevocationOutbox.id)
                .where(due)
                .order_by(AppleRevocationOutbox.created_at)
                .limit(limit)
            ).scalars()
        )  # noqa: E501
    dispatched = 0
    for outbox_id in ids:
        try:
            revoke_apple_credential.apply_async(args=[str(outbox_id)])
            dispatched += 1
        except Exception:  # noqa: BLE001 -- durable row is retried by Beat
            log.warning("apple_revocation_dispatch_failed", outbox_id=str(outbox_id))
    return {"dispatched": dispatched}
