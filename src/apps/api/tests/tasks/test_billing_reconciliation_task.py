from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError, ProgrammingError

from app.config import settings
from app.database import sync_session
from app.models import BillingReconciliation
from app.services.billing_reconciliation import ReconciliationResult
from app.tasks import billing_reconciliation

_REAL_RECONCILIATION_DATES = billing_reconciliation._reconciliation_dates


def _seed_matched_lookback(target: date, *, exclude: set[date]) -> None:
    oldest = target - timedelta(days=billing_reconciliation._RECONCILIATION_LOOKBACK_DAYS)
    with sync_session() as db:
        db.execute(
            delete(BillingReconciliation).where(
                BillingReconciliation.usage_date >= oldest,
                BillingReconciliation.usage_date <= target,
            )
        )
        for offset in range(1, billing_reconciliation._RECONCILIATION_LOOKBACK_DAYS + 1):
            usage_date = target - timedelta(days=offset)
            if usage_date in exclude:
                continue
            db.add(
                BillingReconciliation(
                    usage_date=usage_date,
                    status="matched",
                    threshold_pct=settings.billing_reconciliation_threshold_pct,
                    cloud_costs_usd={},
                    ledger_costs_usd={},
                    differences={},
                    export_watermark=datetime.now(UTC),
                )
            )
        db.commit()


@pytest.fixture(autouse=True)
def _single_reconciliation_day(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        billing_reconciliation,
        "_reconciliation_dates",
        lambda target: [target],
    )


def test_actionable_incomplete_reconciliation_alerts() -> None:
    unmapped = ReconciliationResult(
        usage_date=datetime.now(UTC).date(),
        status="incomplete",
        cloud_costs_usd={"unmapped": 1.0},
        ledger_costs_usd={},
        differences={"unmapped": {"threshold_exceeded": True}},
        export_watermark=datetime.now(UTC),
    )
    missing_export = ReconciliationResult(
        usage_date=datetime.now(UTC).date(),
        status="incomplete",
        cloud_costs_usd={},
        ledger_costs_usd={},
        differences={},
        export_watermark=None,
    )

    assert billing_reconciliation._actionable_reconciliation(unmapped)
    assert billing_reconciliation._actionable_reconciliation(missing_export)


@pytest.fixture
def reconciliation_day(monkeypatch: pytest.MonkeyPatch):
    db_name = make_url(settings.database_url).database or ""
    if not db_name.endswith("_test"):
        pytest.skip(f"refusing to write to non-test database {db_name!r}")
    try:
        with sync_session() as probe:
            probe.execute(text("SELECT 1 FROM billing_reconciliations LIMIT 0"))
    except OperationalError as exc:
        pytest.skip(f"Postgres not reachable for billing integration test: {exc!r}")
    except ProgrammingError as exc:
        pytest.fail(f"nova_test was not migrated to the billing schema: {exc!r}")

    usage_date = (
        datetime.now(UTC) - timedelta(days=settings.billing_reconciliation_delay_days)
    ).date()
    with sync_session() as db:
        db.execute(
            delete(BillingReconciliation).where(BillingReconciliation.usage_date == usage_date)
        )
        db.commit()
    monkeypatch.setattr(settings, "billing_reconciliation_enabled", True)
    yield usage_date
    with sync_session() as db:
        db.execute(
            delete(BillingReconciliation).where(BillingReconciliation.usage_date == usage_date)
        )
        db.commit()


def test_mismatch_is_persisted_and_alerted_exactly_once(
    reconciliation_day, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = ReconciliationResult(
        usage_date=reconciliation_day,
        status="mismatch",
        cloud_costs_usd={"production": 1.0},
        ledger_costs_usd={"production": 0.5},
        differences={
            "production": {
                "cloud_usd": 1.0,
                "ledger_usd": 0.5,
                "relative_difference": 0.5,
                "threshold_exceeded": True,
            }
        },
        export_watermark=datetime.now(UTC),
    )
    publish = MagicMock()
    monkeypatch.setattr(billing_reconciliation, "reconcile_usage_date", lambda _date: result)
    monkeypatch.setattr(billing_reconciliation, "publish_reconciliation_alert", publish)

    first = billing_reconciliation.reconcile_ai_billing.run()
    second = billing_reconciliation.reconcile_ai_billing.run()

    assert first == {
        "usage_date": reconciliation_day.isoformat(),
        "status": "mismatch",
        "alerted": True,
    }
    assert second == {
        "usage_date": reconciliation_day.isoformat(),
        "status": "mismatch",
        "alerted": False,
    }
    publish.assert_called_once_with(result)
    with sync_session() as db:
        persisted = db.scalar(
            select(BillingReconciliation).where(
                BillingReconciliation.usage_date == reconciliation_day
            )
        )
        assert persisted is not None
        assert persisted.alerted_at is not None
        assert persisted.cloud_costs_usd == {"production": 1.0}


def test_alert_failure_leaves_row_retryable(
    reconciliation_day, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = ReconciliationResult(
        usage_date=reconciliation_day,
        status="mismatch",
        cloud_costs_usd={"lab": 0.2},
        ledger_costs_usd={"lab": 0.0},
        differences={"lab": {"threshold_exceeded": True}},
        export_watermark=datetime.now(UTC),
    )
    publish = MagicMock(side_effect=[RuntimeError("pubsub unavailable"), None])
    monkeypatch.setattr(billing_reconciliation, "reconcile_usage_date", lambda _date: result)
    monkeypatch.setattr(billing_reconciliation, "publish_reconciliation_alert", publish)

    first = billing_reconciliation.reconcile_ai_billing.run()
    second = billing_reconciliation.reconcile_ai_billing.run()

    assert first is not None and first["alerted"] is False
    assert second is not None and second["alerted"] is True
    assert publish.call_count == 2


def test_alert_claim_is_atomic_and_durable(
    reconciliation_day, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = ReconciliationResult(
        usage_date=reconciliation_day,
        status="mismatch",
        cloud_costs_usd={"development": 0.2},
        ledger_costs_usd={"development": 0.0},
        differences={"development": {"threshold_exceeded": True}},
        export_watermark=datetime.now(UTC),
    )

    row_id, first_claim = billing_reconciliation._persist(result)
    same_row_id, overlapping_claim = billing_reconciliation._persist(result)

    assert same_row_id == row_id
    assert first_claim is not None
    assert overlapping_claim is None
    with sync_session() as db:
        row = db.get(BillingReconciliation, row_id)
        assert row is not None
        assert row.alert_claim_id == first_claim
        assert row.alert_claim_expires_at is not None

    billing_reconciliation._release_alert_claim(row_id, first_claim)
    _, retry_claim = billing_reconciliation._persist(result)
    assert retry_claim is not None
    assert retry_claim != first_claim


def test_terminal_query_failure_is_persisted_alerted_and_not_raised(
    reconciliation_day, monkeypatch: pytest.MonkeyPatch
) -> None:
    query = MagicMock(side_effect=RuntimeError("billing export unavailable"))
    publish = MagicMock()
    monkeypatch.setattr(billing_reconciliation, "reconcile_usage_date", query)
    monkeypatch.setattr(billing_reconciliation, "publish_reconciliation_alert", publish)

    result = billing_reconciliation.reconcile_ai_billing.run()

    assert result == {
        "usage_date": reconciliation_day.isoformat(),
        "status": "failed",
        "alerted": True,
    }
    published = publish.call_args.args[0]
    assert published.status == "failed"
    assert published.error_detail == "RuntimeError: billing export unavailable"
    with sync_session() as db:
        row = db.scalar(
            select(BillingReconciliation).where(
                BillingReconciliation.usage_date == reconciliation_day
            )
        )
        assert row is not None
        assert row.status == "failed"
        assert row.alerted_at is not None
        assert row.error_detail == "RuntimeError: billing export unavailable"


def test_unsent_failure_alert_is_retried_before_reconciliation_is_overwritten(
    reconciliation_day, monkeypatch: pytest.MonkeyPatch
) -> None:
    query = MagicMock(side_effect=RuntimeError("billing export unavailable"))
    publish = MagicMock(side_effect=[RuntimeError("pubsub down"), None])
    monkeypatch.setattr(billing_reconciliation, "reconcile_usage_date", query)
    monkeypatch.setattr(billing_reconciliation, "publish_reconciliation_alert", publish)

    first = billing_reconciliation.reconcile_ai_billing.run()
    second = billing_reconciliation.reconcile_ai_billing.run()

    assert first is not None and first["alerted"] is False
    assert second is not None and second["alerted"] is True
    assert query.call_count == 1
    assert publish.call_count == 2


def test_reconciliation_dates_includes_bounded_recent_failed_backlog(
    reconciliation_day,
) -> None:
    # Exercise the real selector rather than the autouse single-day shim.
    older = reconciliation_day - timedelta(days=2)
    _seed_matched_lookback(reconciliation_day, exclude={older})
    with sync_session() as db:
        db.add(
            BillingReconciliation(
                usage_date=older,
                status="failed",
                threshold_pct=settings.billing_reconciliation_threshold_pct,
                cloud_costs_usd={},
                ledger_costs_usd={},
                differences={},
                error_detail="prior failure",
            )
        )
        db.commit()
    try:
        dates = _REAL_RECONCILIATION_DATES(reconciliation_day)
        assert older in dates
        assert reconciliation_day in dates
        assert len(dates) <= billing_reconciliation._RECONCILIATION_BATCH
    finally:
        with sync_session() as db:
            db.execute(
                delete(BillingReconciliation).where(BillingReconciliation.usage_date == older)
            )
            db.commit()


def test_reconciliation_dates_recovers_missing_prior_days(reconciliation_day) -> None:
    missing = reconciliation_day - timedelta(days=1)
    with sync_session() as db:
        db.execute(
            delete(BillingReconciliation).where(
                BillingReconciliation.usage_date
                >= reconciliation_day
                - timedelta(days=billing_reconciliation._RECONCILIATION_LOOKBACK_DAYS),
                BillingReconciliation.usage_date <= reconciliation_day,
            )
        )
        for offset in range(2, billing_reconciliation._RECONCILIATION_LOOKBACK_DAYS + 1):
            usage_date = reconciliation_day - timedelta(days=offset)
            db.add(
                BillingReconciliation(
                    usage_date=usage_date,
                    status="matched",
                    threshold_pct=settings.billing_reconciliation_threshold_pct,
                    cloud_costs_usd={},
                    ledger_costs_usd={},
                    differences={},
                    export_watermark=datetime.now(UTC),
                )
            )
        db.commit()

    dates = _REAL_RECONCILIATION_DATES(reconciliation_day)

    assert missing in dates
    assert reconciliation_day in dates
    assert len(dates) <= billing_reconciliation._RECONCILIATION_BATCH

    with sync_session() as db:
        db.execute(
            delete(BillingReconciliation).where(
                BillingReconciliation.usage_date
                >= reconciliation_day
                - timedelta(days=billing_reconciliation._RECONCILIATION_LOOKBACK_DAYS),
                BillingReconciliation.usage_date < reconciliation_day,
            )
        )
        db.commit()


def test_reconciliation_dates_drains_a_full_lookback_after_long_outage(
    reconciliation_day,
) -> None:
    oldest_missing = reconciliation_day - timedelta(
        days=billing_reconciliation._RECONCILIATION_LOOKBACK_DAYS
    )
    with sync_session() as db:
        db.execute(
            delete(BillingReconciliation).where(
                BillingReconciliation.usage_date >= oldest_missing,
                BillingReconciliation.usage_date <= reconciliation_day,
            )
        )
        db.commit()

    dates = _REAL_RECONCILIATION_DATES(reconciliation_day)

    assert oldest_missing in dates
    assert reconciliation_day in dates
    assert len(dates) == billing_reconciliation._RECONCILIATION_BATCH


def test_missing_dates_are_not_starved_by_persistent_failed_rows(
    reconciliation_day,
) -> None:
    missing = reconciliation_day - timedelta(days=1)
    oldest = reconciliation_day - timedelta(
        days=billing_reconciliation._RECONCILIATION_LOOKBACK_DAYS
    )
    with sync_session() as db:
        db.execute(
            delete(BillingReconciliation).where(
                BillingReconciliation.usage_date >= oldest,
                BillingReconciliation.usage_date <= reconciliation_day,
            )
        )
        for offset in range(2, 10):
            db.add(
                BillingReconciliation(
                    usage_date=reconciliation_day - timedelta(days=offset),
                    status="failed",
                    threshold_pct=settings.billing_reconciliation_threshold_pct,
                    cloud_costs_usd={},
                    ledger_costs_usd={},
                    differences={},
                    error_detail="persistent export failure",
                )
            )
        db.commit()

    dates = _REAL_RECONCILIATION_DATES(reconciliation_day)

    assert missing in dates
    assert reconciliation_day in dates


def test_oldest_actionable_row_keeps_a_slot_beside_missing_backlog(
    reconciliation_day,
) -> None:
    boundary = reconciliation_day - timedelta(
        days=billing_reconciliation._RECONCILIATION_LOOKBACK_DAYS
    )
    with sync_session() as db:
        db.execute(
            delete(BillingReconciliation).where(
                BillingReconciliation.usage_date >= boundary,
                BillingReconciliation.usage_date <= reconciliation_day,
            )
        )
        db.add(
            BillingReconciliation(
                usage_date=boundary,
                status="mismatch",
                threshold_pct=settings.billing_reconciliation_threshold_pct,
                cloud_costs_usd={"production": 1.0},
                ledger_costs_usd={"production": 0.0},
                differences={"production": {"threshold_exceeded": True}},
                export_watermark=datetime.now(UTC),
            )
        )
        db.commit()

    dates = _REAL_RECONCILIATION_DATES(reconciliation_day)

    assert boundary in dates
    assert reconciliation_day in dates
    assert len(dates) == billing_reconciliation._RECONCILIATION_BATCH


def test_reconciliation_dates_retries_prior_unsent_mismatch(reconciliation_day) -> None:
    older = reconciliation_day - timedelta(days=2)
    _seed_matched_lookback(reconciliation_day, exclude={older})
    with sync_session() as db:
        db.add(
            BillingReconciliation(
                usage_date=older,
                status="mismatch",
                threshold_pct=settings.billing_reconciliation_threshold_pct,
                cloud_costs_usd={"development": 1.0},
                ledger_costs_usd={"development": 0.0},
                differences={"development": {"threshold_exceeded": True}},
                export_watermark=datetime.now(UTC),
            )
        )
        db.commit()
    try:
        dates = _REAL_RECONCILIATION_DATES(reconciliation_day)
        assert older in dates
    finally:
        with sync_session() as db:
            db.execute(
                delete(BillingReconciliation).where(BillingReconciliation.usage_date == older)
            )
            db.commit()
