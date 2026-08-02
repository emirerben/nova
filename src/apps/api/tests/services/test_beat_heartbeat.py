"""Tests for app/services/beat_heartbeat.py.

This module is the ONLY mechanism that can detect Celery Beat being fully
dead (see the module docstring). Tests focus on the two failure-direction
guarantees: never claim "healthy" without real evidence, and never crash
the caller regardless of Redis state.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _reset_heartbeat_redis_singleton():
    import app.services.beat_heartbeat as heartbeat_module

    heartbeat_module._heartbeat_redis_client = None
    yield
    heartbeat_module._heartbeat_redis_client = None


def test_record_success_writes_a_timestamp():
    from app.services.beat_heartbeat import _BEAT_HEARTBEAT_KEY, record_beat_task_success

    client = MagicMock()
    with patch("app.services.beat_heartbeat._get_heartbeat_redis", return_value=client):
        record_beat_task_success()

    assert client.set.call_count == 1
    key, value = client.set.call_args[0]
    assert key == _BEAT_HEARTBEAT_KEY
    float(value)  # must parse as a timestamp, never raises


def test_record_success_never_raises_when_redis_unavailable():
    from app.services.beat_heartbeat import record_beat_task_success

    with patch("app.services.beat_heartbeat._get_heartbeat_redis", return_value=None):
        record_beat_task_success()  # must not raise


def test_record_success_never_raises_when_set_fails():
    from app.services.beat_heartbeat import record_beat_task_success

    client = MagicMock()
    client.set.side_effect = ConnectionError("blip")
    with patch("app.services.beat_heartbeat._get_heartbeat_redis", return_value=client):
        record_beat_task_success()  # must not raise


def test_status_never_recorded_is_unhealthy():
    """No heartbeat ever written — treated as unhealthy, not assumed OK.
    Covers both a fresh deploy (legitimately no tick yet) and Beat being
    dead from the start — the safe direction to be wrong in is unhealthy."""
    from app.services.beat_heartbeat import beat_heartbeat_status

    client = MagicMock()
    client.get.return_value = None
    with patch("app.services.beat_heartbeat._get_heartbeat_redis", return_value=client):
        healthy, age = beat_heartbeat_status()

    assert healthy is False
    assert age is None


def test_status_recent_heartbeat_is_healthy():
    from app.services.beat_heartbeat import beat_heartbeat_status

    client = MagicMock()
    with patch("app.services.beat_heartbeat._get_heartbeat_redis", return_value=client):
        with patch("app.services.beat_heartbeat.datetime") as mock_dt:
            mock_dt.now.return_value.timestamp.return_value = 1_000_000.0
            client.get.return_value = "999940.0"  # 60s ago
            with patch("app.services.beat_heartbeat.settings") as mock_settings:
                mock_settings.BEAT_HEARTBEAT_STALE_AFTER_MIN = 10
                healthy, age = beat_heartbeat_status()

    assert healthy is True
    assert age == 60.0


def test_status_stale_heartbeat_is_unhealthy():
    from app.services.beat_heartbeat import beat_heartbeat_status

    client = MagicMock()
    with patch("app.services.beat_heartbeat._get_heartbeat_redis", return_value=client):
        with patch("app.services.beat_heartbeat.datetime") as mock_dt:
            mock_dt.now.return_value.timestamp.return_value = 1_000_000.0
            client.get.return_value = "999000.0"  # 1000s ago, > 10 min threshold
            with patch("app.services.beat_heartbeat.settings") as mock_settings:
                mock_settings.BEAT_HEARTBEAT_STALE_AFTER_MIN = 10
                healthy, age = beat_heartbeat_status()

    assert healthy is False
    assert age == 1000.0


def test_status_unhealthy_when_redis_unavailable():
    from app.services.beat_heartbeat import beat_heartbeat_status

    with patch("app.services.beat_heartbeat._get_heartbeat_redis", return_value=None):
        healthy, age = beat_heartbeat_status()

    assert healthy is False
    assert age is None


def test_status_unhealthy_when_redis_get_raises():
    from app.services.beat_heartbeat import beat_heartbeat_status

    client = MagicMock()
    client.get.side_effect = ConnectionError("blip")
    with patch("app.services.beat_heartbeat._get_heartbeat_redis", return_value=client):
        healthy, age = beat_heartbeat_status()

    assert healthy is False
    assert age is None


def test_status_malformed_value_treated_as_unhealthy():
    """Defensive: a corrupted Redis value must not crash the health route."""
    from app.services.beat_heartbeat import beat_heartbeat_status

    client = MagicMock()
    client.get.return_value = "not-a-number"
    with patch("app.services.beat_heartbeat._get_heartbeat_redis", return_value=client):
        healthy, age = beat_heartbeat_status()

    assert healthy is False
    assert age is None
