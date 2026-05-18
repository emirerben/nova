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

from unittest.mock import patch


class TestSweepStaleJobsWrapsReaper:
    def test_returns_reaper_rowcount(self):
        """Happy path: sweeper returns whatever reap_orphans returns."""
        from app.tasks.maintenance import sweep_stale_jobs

        with patch("app.tasks.maintenance.reap_orphans", return_value=7) as mock_reap:
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

        with patch(
            "app.tasks.maintenance.reap_orphans",
            side_effect=RuntimeError("broker hiccup"),
        ):
            result = sweep_stale_jobs.run()

        assert result == 0


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
