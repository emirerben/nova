"""Tests for the render-worker autostop lifecycle task in app/tasks/maintenance.py.

`_decide_lifecycle_action` is a pure function (no Celery/Redis/Fly I/O) —
tested directly and exhaustively. `manage_render_worker_lifecycle` (the
Celery task) is tested with everything mocked, focused on wiring: does it
call the right things in the right order, does the kill switch make it a
complete no-op, does it never act on unknown state.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.tasks.maintenance import _decide_lifecycle_action

_GRACE_MIN = 10


# ── _decide_lifecycle_action (pure decision logic) ──────────────────────────


def test_unknown_idle_state_takes_no_action():
    """render_worker_idle() returned None (inspect() failed) — never act on
    missing information, matching the reaper's own philosophy."""
    action, new_idle_since = _decide_lifecycle_action(
        idle=None, idle_since=12345.0, now=99999.0, grace_min=_GRACE_MIN
    )
    assert action == "unknown"
    # idle_since is passed through unchanged — we don't know enough to
    # either start or reset a grace timer.
    assert new_idle_since == 12345.0


def test_not_idle_clears_tracking_and_requests_backstop():
    action, new_idle_since = _decide_lifecycle_action(
        idle=False, idle_since=12345.0, now=99999.0, grace_min=_GRACE_MIN
    )
    assert action == "not_idle"
    assert new_idle_since is None


def test_newly_idle_starts_the_grace_clock():
    """First tick observing idle=True (no prior idle_since) — starts timing
    from `now`, doesn't stop yet."""
    now = 100_000.0
    action, new_idle_since = _decide_lifecycle_action(
        idle=True, idle_since=None, now=now, grace_min=_GRACE_MIN
    )
    assert action == "grace"
    assert new_idle_since == now


def test_idle_within_grace_period_keeps_waiting():
    now = 100_000.0
    idle_since = now - (5 * 60)  # 5 minutes ago, grace is 10
    action, new_idle_since = _decide_lifecycle_action(
        idle=True, idle_since=idle_since, now=now, grace_min=_GRACE_MIN
    )
    assert action == "grace"
    assert new_idle_since == idle_since  # unchanged — still the same idle period


def test_idle_past_grace_period_stops():
    now = 100_000.0
    idle_since = now - (11 * 60)  # 11 minutes ago, grace is 10
    action, new_idle_since = _decide_lifecycle_action(
        idle=True, idle_since=idle_since, now=now, grace_min=_GRACE_MIN
    )
    assert action == "stop"
    assert new_idle_since is None  # cleared so we don't re-stop every tick


def test_idle_exactly_at_grace_boundary_stops():
    """>= grace_min, not >, so the boundary tick itself triggers stop."""
    now = 100_000.0
    idle_since = now - (_GRACE_MIN * 60)
    action, _ = _decide_lifecycle_action(
        idle=True, idle_since=idle_since, now=now, grace_min=_GRACE_MIN
    )
    assert action == "stop"


# ── manage_render_worker_lifecycle (Celery task wiring) ─────────────────────


@pytest.fixture(autouse=True)
def _reset_lifecycle_redis_singleton():
    import app.tasks.maintenance as maintenance_module

    maintenance_module._lifecycle_redis_client = None
    yield
    maintenance_module._lifecycle_redis_client = None


def _redis_with_stored_idle_since(value: float | None):
    client = MagicMock()
    client.get.return_value = str(value) if value is not None else None
    return client


@patch("app.services.fly_machines.stop_render_worker")
@patch("app.services.fly_machines.start_render_worker")
@patch("app.services.fly_machines.get_render_worker_state")
@patch("app.services.queue_state.render_worker_idle")
def test_kill_switch_off_is_a_complete_no_op(
    mock_idle: MagicMock,
    mock_get_state: MagicMock,
    mock_start: MagicMock,
    mock_stop: MagicMock,
):
    from app.tasks.maintenance import manage_render_worker_lifecycle

    with patch("app.config.settings") as mock_settings:
        mock_settings.RENDER_AUTOSTOP_ENABLED = False
        result = manage_render_worker_lifecycle()

    assert result == "disabled"
    mock_idle.assert_not_called()
    mock_get_state.assert_not_called()
    mock_start.assert_not_called()
    mock_stop.assert_not_called()


@patch("app.tasks.maintenance._get_lifecycle_redis")
@patch("app.services.fly_machines.stop_render_worker")
@patch("app.services.fly_machines.start_render_worker")
@patch("app.services.fly_machines.get_render_worker_state")
@patch("app.services.queue_state.render_worker_idle")
def test_unknown_state_never_calls_fly(
    mock_idle: MagicMock,
    mock_get_state: MagicMock,
    mock_start: MagicMock,
    mock_stop: MagicMock,
    mock_get_redis: MagicMock,
):
    from app.tasks.maintenance import manage_render_worker_lifecycle

    mock_idle.return_value = None
    mock_get_redis.return_value = _redis_with_stored_idle_since(None)

    with patch("app.config.settings") as mock_settings:
        mock_settings.RENDER_AUTOSTOP_ENABLED = True
        mock_settings.RENDER_IDLE_GRACE_MIN = _GRACE_MIN
        result = manage_render_worker_lifecycle()

    assert result == "unknown"
    mock_start.assert_not_called()
    mock_stop.assert_not_called()
    mock_get_state.assert_not_called()


@patch("app.tasks.maintenance._get_lifecycle_redis")
@patch("app.services.fly_machines.stop_render_worker")
@patch("app.services.fly_machines.start_render_worker")
@patch("app.services.fly_machines.get_render_worker_state")
@patch("app.services.queue_state.render_worker_idle")
def test_not_idle_and_machine_not_started_triggers_backstop_start(
    mock_idle: MagicMock,
    mock_get_state: MagicMock,
    mock_start: MagicMock,
    mock_stop: MagicMock,
    mock_get_redis: MagicMock,
):
    from app.tasks.maintenance import manage_render_worker_lifecycle

    mock_idle.return_value = False
    mock_get_state.return_value = "stopped"
    mock_get_redis.return_value = _redis_with_stored_idle_since(None)

    with patch("app.config.settings") as mock_settings:
        mock_settings.RENDER_AUTOSTOP_ENABLED = True
        mock_settings.RENDER_IDLE_GRACE_MIN = _GRACE_MIN
        result = manage_render_worker_lifecycle()

    assert result == "not_idle"
    mock_start.assert_called_once()
    mock_stop.assert_not_called()


@patch("app.tasks.maintenance._get_lifecycle_redis")
@patch("app.services.fly_machines.stop_render_worker")
@patch("app.services.fly_machines.start_render_worker")
@patch("app.services.fly_machines.get_render_worker_state")
@patch("app.services.queue_state.render_worker_idle")
def test_not_idle_and_machine_already_started_does_not_redundantly_call_start(
    mock_idle: MagicMock,
    mock_get_state: MagicMock,
    mock_start: MagicMock,
    mock_stop: MagicMock,
    mock_get_redis: MagicMock,
):
    from app.tasks.maintenance import manage_render_worker_lifecycle

    mock_idle.return_value = False
    mock_get_state.return_value = "started"
    mock_get_redis.return_value = _redis_with_stored_idle_since(None)

    with patch("app.config.settings") as mock_settings:
        mock_settings.RENDER_AUTOSTOP_ENABLED = True
        mock_settings.RENDER_IDLE_GRACE_MIN = _GRACE_MIN
        result = manage_render_worker_lifecycle()

    assert result == "not_idle"
    mock_start.assert_not_called()


@patch("app.tasks.maintenance._get_lifecycle_redis")
@patch("app.services.fly_machines.stop_render_worker")
@patch("app.services.fly_machines.start_render_worker")
@patch("app.services.fly_machines.get_render_worker_state")
@patch("app.services.queue_state.render_worker_idle")
def test_idle_past_grace_stops_and_clears_redis_key(
    mock_idle: MagicMock,
    mock_get_state: MagicMock,
    mock_start: MagicMock,
    mock_stop: MagicMock,
    mock_get_redis: MagicMock,
):
    from app.tasks.maintenance import _RENDER_WORKER_IDLE_SINCE_KEY, manage_render_worker_lifecycle

    mock_idle.return_value = True
    mock_get_state.return_value = "started"
    redis_client = _redis_with_stored_idle_since(0.0)  # very old — definitely past grace
    mock_get_redis.return_value = redis_client

    with (
        patch("app.config.settings") as mock_settings,
        patch("app.tasks.maintenance.datetime") as mock_dt,
    ):
        mock_settings.RENDER_AUTOSTOP_ENABLED = True
        mock_settings.RENDER_IDLE_GRACE_MIN = _GRACE_MIN
        mock_dt.now.return_value.timestamp.return_value = 999_999_999.0
        result = manage_render_worker_lifecycle()

    assert result == "stop"
    mock_stop.assert_called_once()
    mock_start.assert_not_called()
    redis_client.delete.assert_called_once_with(_RENDER_WORKER_IDLE_SINCE_KEY)


@patch("app.tasks.maintenance._get_lifecycle_redis")
@patch("app.services.fly_machines.stop_render_worker")
@patch("app.services.fly_machines.start_render_worker")
@patch("app.services.fly_machines.get_render_worker_state")
@patch("app.services.queue_state.render_worker_idle")
def test_idle_within_grace_neither_starts_nor_stops(
    mock_idle: MagicMock,
    mock_get_state: MagicMock,
    mock_start: MagicMock,
    mock_stop: MagicMock,
    mock_get_redis: MagicMock,
):
    from app.tasks.maintenance import manage_render_worker_lifecycle

    mock_idle.return_value = True
    mock_get_state.return_value = "started"
    mock_get_redis.return_value = _redis_with_stored_idle_since(None)  # first idle tick

    with patch("app.config.settings") as mock_settings:
        mock_settings.RENDER_AUTOSTOP_ENABLED = True
        mock_settings.RENDER_IDLE_GRACE_MIN = _GRACE_MIN
        result = manage_render_worker_lifecycle()

    assert result == "grace"
    mock_start.assert_not_called()
    mock_stop.assert_not_called()


@patch("app.tasks.maintenance._get_lifecycle_redis")
@patch("app.services.fly_machines.stop_render_worker")
@patch("app.services.fly_machines.start_render_worker")
@patch("app.services.fly_machines.get_render_worker_state")
@patch("app.services.queue_state.render_worker_idle")
def test_redis_unavailable_still_makes_a_safe_decision(
    mock_idle: MagicMock,
    mock_get_state: MagicMock,
    mock_start: MagicMock,
    mock_stop: MagicMock,
    mock_get_redis: MagicMock,
):
    """No Redis at all → idle_since always reads as None, so an idle tick
    always lands in "grace" (never stops on the very first tick it can't
    track duration for) rather than crashing."""
    from app.tasks.maintenance import manage_render_worker_lifecycle

    mock_idle.return_value = True
    mock_get_state.return_value = "started"
    mock_get_redis.return_value = None

    with patch("app.config.settings") as mock_settings:
        mock_settings.RENDER_AUTOSTOP_ENABLED = True
        mock_settings.RENDER_IDLE_GRACE_MIN = _GRACE_MIN
        result = manage_render_worker_lifecycle()

    assert result == "grace"
    mock_stop.assert_not_called()


def test_idle_while_machine_stopped_does_not_run_the_grace_clock():
    """Regression (prod 2026-08-23): idle_since accumulated across the
    stopped period, so a wake-for-a-short-task was stopped 37s after it
    started because the timer was already ≥grace old."""
    for state in ("stopped", "stopping", "starting", "replacing"):
        action, new_idle_since = _decide_lifecycle_action(
            idle=True, idle_since=0.0, now=999_999.0, grace_min=_GRACE_MIN, machine_state=state
        )
        assert action == "idle_stopped", state
        assert new_idle_since is None, state


def test_idle_with_unknown_machine_state_behaves_as_started():
    action, new_idle_since = _decide_lifecycle_action(
        idle=True, idle_since=None, now=1000.0, grace_min=_GRACE_MIN, machine_state=None
    )
    assert action == "grace"
    assert new_idle_since == 1000.0


@patch("app.tasks.maintenance._get_lifecycle_redis")
@patch("app.services.fly_machines.stop_render_worker")
@patch("app.services.fly_machines.start_render_worker")
@patch("app.services.fly_machines.get_render_worker_state")
@patch("app.services.queue_state.render_worker_idle")
def test_idle_and_machine_stopped_clears_stale_timer_and_calls_nothing(
    mock_idle: MagicMock,
    mock_get_state: MagicMock,
    mock_start: MagicMock,
    mock_stop: MagicMock,
    mock_get_redis: MagicMock,
):
    from app.tasks.maintenance import _RENDER_WORKER_IDLE_SINCE_KEY, manage_render_worker_lifecycle

    mock_idle.return_value = True
    mock_get_state.return_value = "stopped"
    redis_client = _redis_with_stored_idle_since(0.0)  # stale, ≥grace old
    mock_get_redis.return_value = redis_client

    with (
        patch("app.config.settings") as mock_settings,
        patch("app.tasks.maintenance.datetime") as mock_dt,
    ):
        mock_settings.RENDER_AUTOSTOP_ENABLED = True
        mock_settings.RENDER_IDLE_GRACE_MIN = _GRACE_MIN
        mock_dt.now.return_value.timestamp.return_value = 999_999_999.0
        result = manage_render_worker_lifecycle()

    assert result == "idle_stopped"
    mock_stop.assert_not_called()
    mock_start.assert_not_called()
    redis_client.delete.assert_called_once_with(_RENDER_WORKER_IDLE_SINCE_KEY)
