"""Tests for the task_success heartbeat signal handler in app/worker.py."""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def test_beat_scheduled_task_names_matches_beat_schedule():
    """Regression pin: BEAT_SCHEDULED_TASK_NAMES is derived from
    beat_schedule, not hand-duplicated — this test would fail if a future
    refactor split the two definitions apart."""
    from app.worker import BEAT_SCHEDULED_TASK_NAMES, celery_app

    expected = {entry["task"] for entry in celery_app.conf.beat_schedule.values()}
    assert BEAT_SCHEDULED_TASK_NAMES == expected
    assert "tasks.manage_render_worker_lifecycle" in BEAT_SCHEDULED_TASK_NAMES


@patch("app.services.beat_heartbeat.record_beat_task_success")
def test_beat_scheduled_task_success_records_heartbeat(mock_record: MagicMock):
    from app.worker import _record_beat_heartbeat

    sender = MagicMock()
    sender.name = "tasks.sweep_stale_jobs"
    _record_beat_heartbeat(sender=sender)

    mock_record.assert_called_once()


@patch("app.services.beat_heartbeat.record_beat_task_success")
def test_non_beat_task_success_does_not_record_heartbeat(mock_record: MagicMock):
    """A render job succeeding must NOT count as evidence Beat is alive —
    that would mask an actually-dead Beat process behind normal traffic."""
    from app.worker import _record_beat_heartbeat

    sender = MagicMock()
    sender.name = "orchestrate_generative_job"
    _record_beat_heartbeat(sender=sender)

    mock_record.assert_not_called()


def test_heartbeat_record_failure_does_not_propagate():
    from app.worker import _record_beat_heartbeat

    sender = MagicMock()
    sender.name = "tasks.sweep_stale_jobs"
    with patch(
        "app.services.beat_heartbeat.record_beat_task_success",
        side_effect=RuntimeError("boom"),
    ):
        _record_beat_heartbeat(sender=sender)  # must not raise
