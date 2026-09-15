"""Durability behavior for the Apple deletion-revocation outbox."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.tasks import apple_revocation as task


def _row(*, attempt: int = 0, lease: datetime | None = None, next_at: datetime | None = None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        encrypted_refresh_token=b"ciphertext",
        client_id="com.kria.ios",
        attempts=attempt,
        lease_until=lease,
        next_attempt_at=next_at,
        last_error_code=None,
    )


def _context(row):
    session = MagicMock()
    session.execute.return_value.scalar_one_or_none.return_value = row
    context = MagicMock()
    context.__enter__.return_value = session
    context.__exit__.return_value = False
    return context, session


def test_failed_revoke_keeps_ciphertext_and_schedules_backoff(monkeypatch) -> None:
    row = _row()
    first, first_session = _context(row)
    second, second_session = _context(row)
    monkeypatch.setattr(task, "sync_session", MagicMock(side_effect=[first, second]))
    monkeypatch.setattr(task, "revoke_refresh_token", lambda *_: False)
    before = datetime.now(UTC)
    assert task.revoke_apple_credential.run(str(row.id)) == {"status": "pending"}
    assert row.encrypted_refresh_token == b"ciphertext"
    assert row.attempts == 1 and row.last_error_code == "apple_revoke_failed"
    assert row.next_attempt_at >= before + timedelta(seconds=59)
    assert first_session.commit.called and second_session.commit.called


def test_successful_revoke_removes_only_confirmed_outbox_row(monkeypatch) -> None:
    row = _row()
    first, _ = _context(row)
    second, second_session = _context(row)
    monkeypatch.setattr(task, "sync_session", MagicMock(side_effect=[first, second]))
    monkeypatch.setattr(task, "revoke_refresh_token", lambda *_: True)
    assert task.revoke_apple_credential.run(str(row.id)) == {"status": "revoked"}
    second_session.delete.assert_called_once_with(row)


def test_claim_skips_active_lease_and_recovers_expired_lease(monkeypatch) -> None:
    active = _row(lease=datetime.now(UTC) + timedelta(minutes=1))
    active_context, _ = _context(active)
    monkeypatch.setattr(task, "sync_session", lambda: active_context)
    assert task._claim(str(active.id)) is None
    assert active.attempts == 0

    expired = _row(lease=datetime.now(UTC) - timedelta(seconds=1))
    expired_context, expired_session = _context(expired)
    monkeypatch.setattr(task, "sync_session", lambda: expired_context)
    assert task._claim(str(expired.id)) == (b"ciphertext", "com.kria.ios", 1)
    assert expired.lease_until > datetime.now(UTC)
    expired_session.commit.assert_called_once()


def test_late_worker_result_cannot_overwrite_a_newer_claim(monkeypatch) -> None:
    row = _row()
    first, _ = _context(row)
    # The second transaction observes that another worker has reclaimed it.
    superseded = _row(attempt=2)
    superseded.id = row.id
    second, second_session = _context(superseded)
    monkeypatch.setattr(task, "sync_session", MagicMock(side_effect=[first, second]))
    monkeypatch.setattr(task, "revoke_refresh_token", lambda *_: True)
    assert task.revoke_apple_credential.run(str(row.id)) == {"status": "superseded"}
    second_session.delete.assert_not_called()


def test_sweep_excludes_active_leases_and_respects_backoff(monkeypatch) -> None:
    context = MagicMock()
    session = MagicMock()
    ids = [uuid.uuid4()]
    session.execute.return_value.scalars.return_value = ids
    context.__enter__.return_value = session
    context.__exit__.return_value = False
    monkeypatch.setattr(task, "sync_session", lambda: context)
    published = MagicMock(side_effect=RuntimeError("broker down"))
    monkeypatch.setattr(task.revoke_apple_credential, "apply_async", published)
    assert task.sweep_apple_revocations.run(limit=10) == {"dispatched": 0}
    sql = str(session.execute.call_args.args[0])
    assert "AND" in sql and "lease_until IS NULL" in sql
    published.assert_called_once_with(args=[str(ids[0])])
