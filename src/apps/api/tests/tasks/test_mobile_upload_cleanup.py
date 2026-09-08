from __future__ import annotations

import uuid
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.tasks import mobile_upload_cleanup


def _reservation(*, now: datetime) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        object_path="analysis-proxy/user/upload/clip.mp4",
        status="reserved",
        retention_expires_at=now - timedelta(minutes=1),
        cleanup_claimed_at=None,
        deleted_at=None,
        delete_attempts=0,
        last_error=None,
    )


def _sessions(monkeypatch, row: SimpleNamespace) -> tuple[MagicMock, MagicMock]:
    claim_session = MagicMock()
    claim_session.execute.return_value.scalars.return_value.all.return_value = [row]
    receipt_session = MagicMock()
    receipt_session.get.return_value = row
    pending = iter((claim_session, receipt_session))
    monkeypatch.setattr(
        mobile_upload_cleanup,
        "sync_session",
        lambda: nullcontext(next(pending)),
    )
    return claim_session, receipt_session


def test_cleanup_claims_and_receipts_a_deleted_object(monkeypatch) -> None:
    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    row = _reservation(now=now)
    claim_session, receipt_session = _sessions(monkeypatch, row)
    deleted_paths: list[str] = []
    monkeypatch.setattr(
        mobile_upload_cleanup,
        "delete_object_best_effort",
        lambda path: deleted_paths.append(path) is None,
    )

    deleted = mobile_upload_cleanup.cleanup_expired_temporary_uploads(now=now)

    assert deleted == 1
    assert deleted_paths == [row.object_path]
    assert row.status == "deleted"
    assert row.cleanup_claimed_at == now
    assert row.delete_attempts == 1
    assert row.deleted_at is not None
    assert row.last_error is None
    claim_session.commit.assert_called_once_with()
    receipt_session.commit.assert_called_once_with()
    receipt_session.get.assert_called_once_with(
        mobile_upload_cleanup.TemporaryMediaUpload,
        row.id,
        with_for_update=True,
    )


def test_cleanup_failure_stays_pending_for_bounded_retry(monkeypatch) -> None:
    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    row = _reservation(now=now)
    _sessions(monkeypatch, row)
    monkeypatch.setattr(
        mobile_upload_cleanup,
        "delete_object_best_effort",
        lambda _path: False,
    )

    deleted = mobile_upload_cleanup.cleanup_expired_temporary_uploads(now=now)

    assert deleted == 0
    assert row.status == "cleanup_pending"
    assert row.cleanup_claimed_at == now
    assert row.delete_attempts == 1
    assert row.deleted_at is None
    assert row.last_error == "storage_unavailable"


def test_cleanup_zero_limit_never_opens_a_database_session(monkeypatch) -> None:
    session_factory = MagicMock()
    monkeypatch.setattr(mobile_upload_cleanup, "sync_session", session_factory)

    assert mobile_upload_cleanup.cleanup_expired_temporary_uploads(limit=0) == 0
    session_factory.assert_not_called()


def test_cleanup_caps_batch_and_ignores_row_changed_after_storage_delete(monkeypatch) -> None:
    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    row = _reservation(now=now)
    claim_session = MagicMock()
    claim_session.execute.return_value.scalars.return_value.all.return_value = [row]
    receipt_session = MagicMock()
    receipt_session.get.return_value = SimpleNamespace(
        id=row.id,
        object_path="different/object.mp4",
        deleted_at=None,
    )
    pending = iter((claim_session, receipt_session))
    monkeypatch.setattr(
        mobile_upload_cleanup,
        "sync_session",
        lambda: nullcontext(next(pending)),
    )
    monkeypatch.setattr(
        mobile_upload_cleanup,
        "delete_object_best_effort",
        lambda _path: True,
    )

    deleted = mobile_upload_cleanup.cleanup_expired_temporary_uploads(now=now, limit=500)

    assert deleted == 0
    statement = claim_session.execute.call_args.args[0]
    assert 50 in statement.compile().params.values()
    receipt_session.commit.assert_not_called()
