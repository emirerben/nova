"""Unit tests for the periodic stale-job sweeper.

The sweeper (app/tasks/maintenance.py) wraps the existing on-boot reaper
(app/tasks/reaper.py) so it runs every 5 min via Celery Beat. This is the
safety net for jobs that escape both:
  1. orchestrator autoretry (DB outage > ~30s)
  2. _mark_failed's internal retry (DB still down by attempt 3)

Without this periodic safety net, zombie rows from such double-failures
stay in the DB until the next deploy / worker restart triggers the
on-boot reaper.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from billiard.exceptions import SoftTimeLimitExceeded

from app.services.durable_attempt_cleanup import CleanupReconcileResult


class TestSweepStaleJobsWrapsReaper:
    def test_returns_reaper_rowcount(self):
        """Happy path: sweeper returns whatever reap_orphans returns."""
        from app.tasks.maintenance import sweep_stale_jobs

        with (
            patch("app.tasks.maintenance._live_job_ids", return_value=set()),
            patch("app.tasks.maintenance.reap_orphans", return_value=7) as mock_reap,
            patch("app.tasks.maintenance.reconcile_stuck_variants"),
            patch("app.tasks.maintenance.reconcile_storage_attempt_receipts"),
        ):
            # Celery `bind=True` tasks expose .run as the unbound body;
            # call .__wrapped__ or .run with a fake self.
            result = sweep_stale_jobs.run()

        assert result == 7
        mock_reap.assert_called_once()
        # Confirm we passed the actual celery_app (not None / a bare module).
        from app.worker import celery_app as expected_app

        assert mock_reap.call_args[0][0] is expected_app

    def test_swallows_reaper_exception_returns_zero(self):
        """Sweeper failure must not crash Beat — log and return 0."""
        from app.tasks.maintenance import sweep_stale_jobs

        with (
            patch(
                "app.tasks.maintenance.reap_orphans",
                side_effect=RuntimeError("broker hiccup"),
            ),
            patch("app.tasks.maintenance.reconcile_storage_attempt_receipts"),
        ):
            result = sweep_stale_jobs.run()

        assert result == 0


def test_storage_attempt_beat_pass_is_indexed_and_bounded() -> None:
    from app.tasks.maintenance import reconcile_storage_attempt_receipts

    job_id = uuid.uuid4()
    session = MagicMock()
    context = MagicMock()
    context.__enter__.return_value = session
    context.__exit__.return_value = False
    select_jobs = MagicMock(return_value=[job_id])
    reconcile = MagicMock(return_value=CleanupReconcileResult(receipts_seen=2))
    cancelled = MagicMock(return_value=SimpleNamespace(cleanup_safe=True))

    with (
        patch("app.tasks.maintenance.sync_session", return_value=context),
        patch(
            "app.tasks.maintenance.jobs_with_storage_attempt_cleanup_receipts",
            select_jobs,
        ),
        patch("app.tasks.maintenance.reconcile_storage_attempt_cleanup", reconcile),
        patch(
            "app.tasks.maintenance.reconcile_cancelled_required_speech_job",
            cancelled,
        ),
    ):
        seen = reconcile_storage_attempt_receipts(limit=99)

    assert seen == 2
    select_jobs.assert_called_once_with(session, limit=1)
    cancelled.assert_called_once_with(job_id)
    reconcile.assert_called_once_with(job_id, source_limit=1, render_limit=1)


def test_storage_attempt_beat_terminalizes_cancelled_owner_before_cleanup() -> None:
    from app.tasks.maintenance import reconcile_storage_attempt_receipts

    job_id = uuid.uuid4()
    order: list[str] = []
    session = MagicMock()
    context = MagicMock()
    context.__enter__.return_value = session
    context.__exit__.return_value = False

    with (
        patch("app.tasks.maintenance.sync_session", return_value=context),
        patch(
            "app.tasks.maintenance.jobs_with_storage_attempt_cleanup_receipts",
            return_value=[job_id],
        ),
        patch(
            "app.tasks.maintenance.reconcile_cancelled_required_speech_job",
            side_effect=lambda _job_id: order.append("terminalize"),
        ),
        patch(
            "app.tasks.maintenance.reconcile_storage_attempt_cleanup",
            side_effect=lambda *_args, **_kwargs: (
                order.append("cleanup") or CleanupReconcileResult()
            ),
        ),
    ):
        reconcile_storage_attempt_receipts()

    assert order == ["terminalize", "cleanup"]


def test_storage_cleanup_soft_deadline_propagates_from_beat_task() -> None:
    from app.tasks.maintenance import sweep_stale_jobs

    with (
        patch(
            "app.tasks.maintenance.reconcile_storage_attempt_receipts",
            side_effect=SoftTimeLimitExceeded(),
        ),
        patch("app.tasks.maintenance._live_job_ids") as inspect,
        pytest.raises(SoftTimeLimitExceeded),
    ):
        sweep_stale_jobs.run()

    inspect.assert_not_called()


def test_cancelled_job_cleanup_reconciles_durable_generations_first() -> None:
    from app.tasks.maintenance import cleanup_cancelled_job

    job_id = str(uuid.uuid4())
    bucket = MagicMock()
    bucket.list_blobs.return_value = []
    client = MagicMock()
    client.bucket.return_value = bucket
    reconcile = MagicMock(return_value=CleanupReconcileResult(receipts_seen=1, deleted=1))

    with (
        patch(
            "app.tasks.maintenance.reconcile_cancelled_required_speech_job",
            return_value=SimpleNamespace(cleanup_safe=True, reason=None),
        ),
        patch("app.tasks.maintenance.reconcile_storage_attempt_cleanup", reconcile),
        patch("app.tasks.maintenance._cancelled_job_storage_quiescent", return_value=True),
        patch("app.storage._get_client", return_value=client),
    ):
        deleted = cleanup_cancelled_job.run(job_id)

    assert deleted == 0
    reconcile.assert_called_once_with(job_id, source_limit=4, render_limit=8)
    assert bucket.list_blobs.call_count > 0


def test_cancelled_job_cleanup_propagates_durable_soft_deadline() -> None:
    from app.tasks.maintenance import cleanup_cancelled_job

    with (
        patch(
            "app.tasks.maintenance.reconcile_cancelled_required_speech_job",
            return_value=SimpleNamespace(cleanup_safe=True, reason=None),
        ),
        patch(
            "app.tasks.maintenance.reconcile_storage_attempt_cleanup",
            side_effect=SoftTimeLimitExceeded(),
        ),
        patch("app.storage._get_client") as get_client,
        pytest.raises(SoftTimeLimitExceeded),
    ):
        cleanup_cancelled_job.run(str(uuid.uuid4()))

    get_client.assert_not_called()


def test_cancelled_job_cleanup_never_reaches_storage_with_fresh_private_owner() -> None:
    from app.tasks.maintenance import cleanup_cancelled_job

    ownership = SimpleNamespace(cleanup_safe=False, reason="generation_uploads_still_active")
    with (
        patch(
            "app.tasks.maintenance.reconcile_cancelled_required_speech_job",
            return_value=ownership,
        ),
        patch("app.tasks.maintenance.reconcile_storage_attempt_cleanup") as reconcile,
        patch("app.storage._get_client") as get_client,
    ):
        assert cleanup_cancelled_job.run(str(uuid.uuid4())) == 0

    reconcile.assert_not_called()
    get_client.assert_not_called()


def test_cancelled_job_cleanup_stops_when_durable_receipt_is_retained() -> None:
    from app.tasks.maintenance import cleanup_cancelled_job

    retained = CleanupReconcileResult(receipts_seen=1, retained=1)
    with (
        patch(
            "app.tasks.maintenance.reconcile_cancelled_required_speech_job",
            return_value=SimpleNamespace(cleanup_safe=True, reason=None),
        ),
        patch(
            "app.tasks.maintenance.reconcile_storage_attempt_cleanup",
            return_value=retained,
        ),
        patch("app.storage._get_client") as get_client,
    ):
        assert cleanup_cancelled_job.run(str(uuid.uuid4())) == 0

    get_client.assert_not_called()


class TestBeatScheduleConfig:
    def test_beat_schedule_has_sweep_entry(self):
        """The Celery Beat schedule must reference tasks.sweep_stale_jobs."""
        from app.worker import celery_app

        schedule = celery_app.conf.beat_schedule or {}
        # Find the entry that targets our sweeper task.
        sweep_entries = [
            (name, cfg)
            for name, cfg in schedule.items()
            if cfg.get("task") == "tasks.sweep_stale_jobs"
        ]
        assert sweep_entries, (
            "beat_schedule must include a tasks.sweep_stale_jobs entry — "
            "without it the periodic safety net never fires."
        )
        # Verify a sensible schedule interval (5 min default, but tolerate
        # 60-3600s in case the cadence is tuned later).
        name, cfg = sweep_entries[0]
        interval = cfg.get("schedule")
        # Schedule can be a number (seconds) or a celery.schedules object.
        if isinstance(interval, (int, float)):
            assert 60 <= interval <= 3600, (
                f"Sweep schedule {interval}s is outside reasonable bounds [60s, 3600s]."
            )
