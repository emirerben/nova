"""Daily delayed Cloud Billing reconciliation task."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import structlog
from sqlalchemy import func, select, text, update

from app.config import settings
from app.database import sync_engine, sync_session
from app.models import BillingReconciliation
from app.services.billing_reconciliation import (
    ReconciliationResult,
    publish_reconciliation_alert,
    reconcile_usage_date,
)
from app.worker import celery_app

log = structlog.get_logger()

_ALERT_CLAIM_LEASE = timedelta(minutes=10)
_RECONCILIATION_LOOKBACK_DAYS = 14
_RECONCILIATION_BATCH = 7


def _actionable_reconciliation(result: ReconciliationResult) -> bool:
    """Return whether operators must be alerted for this delayed usage day."""

    if result.status in {"mismatch", "failed"}:
        return True
    if result.status != "incomplete":
        return False
    if result.export_watermark is None:
        return True
    return any(
        bool(difference.get("threshold_exceeded"))
        for difference in result.differences.values()
        if isinstance(difference, dict)
    )


def _persist(result: ReconciliationResult) -> tuple[uuid.UUID, uuid.UUID | None]:
    """Upsert the result and atomically claim its one per-day alert outbox row."""

    now = datetime.now(UTC)
    with sync_session() as db:
        # The unique usage_date constraint prevents duplicate rows, while this
        # transaction lock also closes the read-then-insert and alert-claim
        # races between overlapping Celery runs.
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"),
            {"scope": f"billing-reconciliation:{result.usage_date.isoformat()}"},
        )
        row = db.scalar(
            select(BillingReconciliation).where(
                BillingReconciliation.usage_date == result.usage_date
            )
        )
        if row is None:
            row = BillingReconciliation(
                usage_date=result.usage_date,
                status=result.status,
                threshold_pct=settings.billing_reconciliation_threshold_pct,
                cloud_costs_usd=result.cloud_costs_usd,
                ledger_costs_usd=result.ledger_costs_usd,
                differences=result.differences,
                export_watermark=result.export_watermark,
                error_detail=result.error_detail,
            )
            db.add(row)
        else:
            row.status = result.status
            row.threshold_pct = settings.billing_reconciliation_threshold_pct
            row.cloud_costs_usd = result.cloud_costs_usd
            row.ledger_costs_usd = result.ledger_costs_usd
            row.differences = result.differences
            row.export_watermark = result.export_watermark
            row.error_detail = result.error_detail
        claim_id: uuid.UUID | None = None
        claim_expired = row.alert_claim_expires_at is None or row.alert_claim_expires_at <= now
        if _actionable_reconciliation(result) and row.alerted_at is None and claim_expired:
            claim_id = uuid.uuid4()
            row.alert_claim_id = claim_id
            row.alert_claim_expires_at = now + _ALERT_CLAIM_LEASE
        db.commit()
        return row.id, claim_id


def _mark_alerted(row_id: uuid.UUID, claim_id: uuid.UUID) -> bool:
    with sync_session() as db:
        claimed = db.execute(
            update(BillingReconciliation)
            .where(
                BillingReconciliation.id == row_id,
                BillingReconciliation.alerted_at.is_(None),
                BillingReconciliation.alert_claim_id == claim_id,
            )
            .values(
                alerted_at=datetime.now(UTC),
                alert_claim_id=None,
                alert_claim_expires_at=None,
            )
        )
        db.commit()
        return claimed.rowcount == 1


def _release_alert_claim(row_id: uuid.UUID, claim_id: uuid.UUID) -> None:
    """Return a synchronously failed publication to the durable outbox."""

    with sync_session() as db:
        db.execute(
            update(BillingReconciliation)
            .where(
                BillingReconciliation.id == row_id,
                BillingReconciliation.alerted_at.is_(None),
                BillingReconciliation.alert_claim_id == claim_id,
            )
            .values(alert_claim_id=None, alert_claim_expires_at=None)
        )
        db.commit()


def _result_from_row(row: BillingReconciliation) -> ReconciliationResult:
    return ReconciliationResult(
        usage_date=row.usage_date,
        status=row.status,
        cloud_costs_usd=dict(row.cloud_costs_usd or {}),
        ledger_costs_usd=dict(row.ledger_costs_usd or {}),
        differences=dict(row.differences or {}),
        export_watermark=row.export_watermark,
        error_detail=row.error_detail,
    )


def _claim_pending_alert(
    usage_date: date,
) -> tuple[uuid.UUID, uuid.UUID | None, ReconciliationResult] | None:
    """Lease an existing unsent alert before overwriting it with a retry."""

    now = datetime.now(UTC)
    with sync_session() as db:
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"),
            {"scope": f"billing-reconciliation:{usage_date.isoformat()}"},
        )
        row = db.scalar(
            select(BillingReconciliation).where(BillingReconciliation.usage_date == usage_date)
        )
        if row is None or row.alerted_at is not None:
            return None
        result = _result_from_row(row)
        if not _actionable_reconciliation(result):
            return None
        if row.alert_claim_expires_at is not None and row.alert_claim_expires_at > now:
            # Another worker owns the durable outbox item. Do not overwrite
            # the evidence it is in the process of publishing.
            return row.id, None, result
        claim_id = uuid.uuid4()
        row.alert_claim_id = claim_id
        row.alert_claim_expires_at = now + _ALERT_CLAIM_LEASE
        db.commit()
        return row.id, claim_id, result


def _publish_claim(
    result: ReconciliationResult,
    *,
    row_id: uuid.UUID,
    claim_id: uuid.UUID | None,
) -> bool:
    if claim_id is None:
        return False
    try:
        publish_reconciliation_alert(result)
    except Exception as exc:  # noqa: BLE001 - release the outbox claim for retry
        _release_alert_claim(row_id, claim_id)
        log.error(
            "billing_reconciliation.alert_failed",
            usage_date=result.usage_date.isoformat(),
            error_type=type(exc).__name__,
        )
        return False
    return _mark_alerted(row_id, claim_id)


def _reconciliation_dates(target: date) -> list[date]:
    """Return a bounded, self-healing backlog through the delayed usage day.

    Missing rows matter as much as explicit failures: Beat or database outages
    can prevent a row from being created at all. Unsent actionable alerts also
    remain work even when their reconciliation status is ``mismatch``.
    """

    start = target - timedelta(days=_RECONCILIATION_LOOKBACK_DAYS)
    now = datetime.now(UTC)
    with sync_session() as db:
        rows = list(
            db.scalars(
                select(BillingReconciliation).where(
                    BillingReconciliation.usage_date >= start,
                    BillingReconciliation.usage_date <= target,
                )
            )
        )
    by_date = {row.usage_date: row for row in rows}
    missing_dates = {
        usage_date
        for offset in range(_RECONCILIATION_LOOKBACK_DAYS + 1)
        if (usage_date := target - timedelta(days=offset)) not in by_date
    }
    actionable_dates: set[date] = set()
    for row in rows:
        result = _result_from_row(row)
        alert_retryable = (
            row.alerted_at is None
            and _actionable_reconciliation(result)
            and (row.alert_claim_expires_at is None or row.alert_claim_expires_at <= now)
        )
        if row.status in {"failed", "incomplete"} or alert_retryable:
            actionable_dates.add(row.usage_date)

    # Always give today's target a slot. First-time missing dates precede most
    # retries because each attempt creates a durable row even on failure;
    # persistent failed rows therefore cannot consume the whole batch.
    # Alternate newest/oldest gaps: yesterday is repaired promptly while the
    # oldest outage date keeps making progress before it ages out. Every
    # processed gap becomes durable and falls out on the next run.
    missing_sorted = sorted(missing_dates - {target})
    missing_queue: list[date] = []
    while missing_sorted:
        missing_queue.append(missing_sorted.pop())
        if missing_sorted:
            missing_queue.append(missing_sorted.pop(0))
    actionable_queue = sorted(actionable_dates - missing_dates - {target})
    prioritized: list[date] = []
    # When both queues are non-empty, reserve one slot for the oldest durable
    # alert/retry before filling with first-time missing days. This guarantees
    # progress for both queues before either can age out of the lookback.
    if missing_queue and actionable_queue:
        prioritized.append(actionable_queue.pop(0))
    prioritized.extend(missing_queue)
    prioritized.extend(actionable_queue)
    return sorted({*prioritized[: _RECONCILIATION_BATCH - 1], target})


def _process_usage_date_locked(usage_date: date) -> dict[str, object]:
    pending = _claim_pending_alert(usage_date)
    if pending is not None:
        row_id, claim_id, result = pending
        return {
            "usage_date": usage_date.isoformat(),
            "status": result.status,
            "alerted": _publish_claim(
                result,
                row_id=row_id,
                claim_id=claim_id,
            ),
        }

    try:
        result = reconcile_usage_date(usage_date)
    except Exception as exc:  # noqa: BLE001 - terminal result is persisted and alerted
        result = ReconciliationResult(
            usage_date=usage_date,
            status="failed",
            cloud_costs_usd={},
            ledger_costs_usd={},
            differences={},
            export_watermark=None,
            error_detail=f"{type(exc).__name__}: {exc}"[:2_000],
        )
        row_id, claim_id = _persist(result)
        alerted = _publish_claim(result, row_id=row_id, claim_id=claim_id)
        log.error(
            "billing_reconciliation.failed",
            usage_date=usage_date.isoformat(),
            error_type=type(exc).__name__,
            alerted=alerted,
        )
        return {
            "usage_date": usage_date.isoformat(),
            "status": result.status,
            "alerted": alerted,
        }

    row_id, claim_id = _persist(result)
    alerted = _publish_claim(result, row_id=row_id, claim_id=claim_id)
    log.info(
        "billing_reconciliation.completed",
        usage_date=usage_date.isoformat(),
        status=result.status,
        differences=result.differences,
    )
    return {
        "usage_date": usage_date.isoformat(),
        "status": result.status,
        "alerted": alerted,
    }


def _process_usage_date(usage_date: date) -> dict[str, object]:
    """Serialize the query→persist→outbox sequence for one provider day."""

    scope = f"billing-reconciliation-run:{usage_date.isoformat()}"
    with sync_engine.connect() as lock_conn:
        lock_conn.execute(select(func.pg_advisory_lock(func.hashtextextended(scope, 0))))
        try:
            return _process_usage_date_locked(usage_date)
        finally:
            lock_conn.execute(select(func.pg_advisory_unlock(func.hashtextextended(scope, 0))))


@celery_app.task(
    name="tasks.reconcile_ai_billing",
    max_retries=0,
    # A backlog pass can make up to seven serialized BigQuery queries plus
    # Pub/Sub publications. Keep a hard bound, but leave enough room for each
    # request's own timeout to expire cleanly and persist a failed result.
    soft_time_limit=600,
    time_limit=660,
)
def reconcile_ai_billing() -> dict[str, object] | None:
    if not settings.billing_reconciliation_enabled:
        return None
    usage_date = (
        datetime.now(UTC) - timedelta(days=settings.billing_reconciliation_delay_days)
    ).date()
    target_result: dict[str, object] | None = None
    for pending_date in _reconciliation_dates(usage_date):
        processed = _process_usage_date(pending_date)
        if pending_date == usage_date:
            target_result = processed
    return target_result
