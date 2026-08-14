from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.tasks import maintenance


class _Rows:
    def __init__(self, rows):  # noqa: ANN001
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _Session:
    def __init__(self, rows):  # noqa: ANN001
        self.rows = rows
        self.commits = 0
        self.deleted = []

    def execute(self, _stmt):
        return _Rows(self.rows)

    def commit(self):
        self.commits += 1

    def delete(self, row):  # noqa: ANN001
        self.deleted.append(row)


def _asset(*, attempts: int):
    return SimpleNamespace(
        id=uuid.uuid4(),
        status="queued",
        analysis_attempt_token="old",
        analysis_attempt_count=attempts,
        analysis_last_dispatched_at=None,
        analysis_started_at=None,
        error_code=None,
        error_detail=None,
        error_retryable=False,
        created_at=datetime(2026, 8, 14, 8, 0, tzinfo=UTC),
        gcs_path=f"users/u/plan/i/pool/{uuid.uuid4()}.png",
    )


def test_reconcile_requeues_with_new_fenced_attempt(monkeypatch) -> None:
    asset = _asset(attempts=1)
    session = _Session([asset])

    @contextmanager
    def _session():
        yield session

    publish = MagicMock()
    monkeypatch.setattr(maintenance, "sync_session", _session)
    monkeypatch.setattr("app.tasks.autoplace.analyze_pool_asset.apply_async", publish)
    monkeypatch.setattr("app.config.settings.pool_asset_analysis_queue", "autoplace-jobs")

    assert maintenance.reconcile_stale_pool_assets(now=datetime.now(UTC)) == 1
    assert asset.status == "queued"
    assert asset.analysis_attempt_count == 2
    assert asset.analysis_attempt_token != "old"
    publish.assert_called_once_with(
        args=[str(asset.id), False, asset.analysis_attempt_token],
        queue="autoplace-jobs",
    )


def test_reconcile_terminalizes_exhausted_attempt(monkeypatch) -> None:
    asset = _asset(attempts=3)
    session = _Session([asset])

    @contextmanager
    def _session():
        yield session

    publish = MagicMock()
    monkeypatch.setattr(maintenance, "sync_session", _session)
    monkeypatch.setattr("app.tasks.autoplace.analyze_pool_asset.apply_async", publish)

    assert maintenance.reconcile_stale_pool_assets(now=datetime.now(UTC)) == 1
    assert asset.status == "failed"
    assert asset.error_code == "analysis_timed_out"
    assert asset.error_retryable is True
    publish.assert_not_called()


def test_reconcile_deletes_expired_reservation_and_object(monkeypatch) -> None:
    asset = _asset(attempts=0)
    asset.status = "preparing"
    session = _Session([asset])

    @contextmanager
    def _session():
        yield session

    cleanup = MagicMock(return_value=True)
    monkeypatch.setattr(maintenance, "sync_session", _session)
    monkeypatch.setattr("app.storage.delete_object_best_effort", cleanup)

    now = datetime(2026, 8, 14, 9, 0, tzinfo=UTC)
    assert maintenance.reconcile_stale_pool_assets(now=now) == 1
    assert session.deleted == [asset]
    cleanup.assert_called_once_with(asset.gcs_path)
