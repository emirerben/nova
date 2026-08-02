"""Tests for the render-worker autostop wake hook in app/worker.py.

`_wake_render_worker_on_dispatch` is a `before_task_publish` signal handler;
`_maybe_start_render_worker` is the debounced Fly-call logic it spawns onto a
background thread. Tested separately: the signal handler's job is "should a
wake even be attempted" (flag + queue filtering), the debounce function's job
is "should the actual Fly call fire" (debounce state). Threading is patched to
run synchronously so these tests are deterministic, not sleep-based.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


class _SyncThread:
    """Stand-in for threading.Thread that runs its target immediately,
    inline, on construction — so tests don't need sleep()/join() timing."""

    def __init__(self, target=None, daemon=None, name=None):
        self._target = target

    def start(self):
        if self._target is not None:
            self._target()


@pytest.fixture(autouse=True)
def _reset_wake_redis_singleton():
    """The module-level Redis singleton must not leak between tests."""
    import app.worker as worker_module

    worker_module._wake_redis_client = None
    yield
    worker_module._wake_redis_client = None


# ── _wake_render_worker_on_dispatch (signal handler: should we even try) ────


@patch("app.worker.threading.Thread", _SyncThread)
@patch("app.worker._maybe_start_render_worker")
def test_flag_off_never_attempts_wake(mock_maybe_start: MagicMock):
    from app.worker import _wake_render_worker_on_dispatch

    with patch("app.worker.settings") as mock_settings:
        mock_settings.RENDER_AUTOSTOP_ENABLED = False
        _wake_render_worker_on_dispatch(sender="orchestrate_generative_job", routing_key="celery")

    mock_maybe_start.assert_not_called()


@patch("app.worker.threading.Thread", _SyncThread)
@patch("app.worker._maybe_start_render_worker")
def test_flag_on_render_queue_attempts_wake(mock_maybe_start: MagicMock):
    from app.worker import _wake_render_worker_on_dispatch

    with patch("app.worker.settings") as mock_settings:
        mock_settings.RENDER_AUTOSTOP_ENABLED = True
        _wake_render_worker_on_dispatch(sender="orchestrate_generative_job", routing_key="celery")

    mock_maybe_start.assert_called_once()


@pytest.mark.parametrize("queue", ["plan-jobs", "overlay-jobs"])
@patch("app.worker.threading.Thread", _SyncThread)
@patch("app.worker._maybe_start_render_worker")
def test_flag_on_other_render_queues_attempt_wake(mock_maybe_start: MagicMock, queue: str):
    from app.worker import _wake_render_worker_on_dispatch

    with patch("app.worker.settings") as mock_settings:
        mock_settings.RENDER_AUTOSTOP_ENABLED = True
        _wake_render_worker_on_dispatch(sender="regenerate_generative_variant", routing_key=queue)

    mock_maybe_start.assert_called_once()


@patch("app.worker.threading.Thread", _SyncThread)
@patch("app.worker._maybe_start_render_worker")
def test_flag_on_maintenance_queue_does_not_wake(mock_maybe_start: MagicMock):
    """A maintenance-lane task (Phase 1 task_routes target) must never wake
    the render worker — that would defeat the whole point of the split."""
    from app.worker import _wake_render_worker_on_dispatch

    with patch("app.worker.settings") as mock_settings:
        mock_settings.RENDER_AUTOSTOP_ENABLED = True
        _wake_render_worker_on_dispatch(sender="tasks.sweep_stale_jobs", routing_key="maintenance")

    mock_maybe_start.assert_not_called()


@patch("app.worker.threading.Thread", _SyncThread)
@patch("app.worker._maybe_start_render_worker")
def test_flag_on_unrecognized_queue_does_not_wake(mock_maybe_start: MagicMock):
    """Any queue outside RENDER_WORKER_QUEUES is treated as non-render,
    including one that doesn't exist yet — this is the structural-closure
    property the routing_key design exists for."""
    from app.worker import _wake_render_worker_on_dispatch

    with patch("app.worker.settings") as mock_settings:
        mock_settings.RENDER_AUTOSTOP_ENABLED = True
        _wake_render_worker_on_dispatch(sender="some_future_task", routing_key="some-new-queue")

    mock_maybe_start.assert_not_called()


@patch("app.worker.threading.Thread", _SyncThread)
def test_exception_inside_background_thread_does_not_propagate(caplog):
    """A crash in _maybe_start_render_worker must be swallowed — this signal
    fires inline in whatever process/thread is publishing the task, and must
    never surface an error to that caller."""
    from app.worker import _wake_render_worker_on_dispatch

    with (
        patch("app.worker.settings") as mock_settings,
        patch("app.worker._maybe_start_render_worker", side_effect=RuntimeError("boom")),
    ):
        mock_settings.RENDER_AUTOSTOP_ENABLED = True
        # Must not raise.
        _wake_render_worker_on_dispatch(sender="orchestrate_generative_job", routing_key="celery")


# ── _maybe_start_render_worker (debounce logic: should the Fly call fire) ──


def _redis_client(set_return=True, set_side_effect=None):
    client = MagicMock()
    if set_side_effect is not None:
        client.set.side_effect = set_side_effect
    else:
        client.set.return_value = set_return
    return client


@patch("app.services.fly_machines.start_render_worker")
@patch("app.worker._get_wake_redis")
def test_debounce_acquired_calls_start(mock_get_redis: MagicMock, mock_start: MagicMock):
    from app.worker import _maybe_start_render_worker

    mock_get_redis.return_value = _redis_client(set_return=True)
    _maybe_start_render_worker()
    mock_start.assert_called_once()


@patch("app.services.fly_machines.start_render_worker")
@patch("app.worker._get_wake_redis")
def test_debounce_not_acquired_skips_start(mock_get_redis: MagicMock, mock_start: MagicMock):
    """Key already present (SET NX failed) — another dispatch woke it recently."""
    from app.worker import _maybe_start_render_worker

    mock_get_redis.return_value = _redis_client(set_return=False)
    _maybe_start_render_worker()
    mock_start.assert_not_called()


@patch("app.services.fly_machines.start_render_worker")
@patch("app.worker._get_wake_redis")
def test_redis_unavailable_fails_open_and_calls_start(
    mock_get_redis: MagicMock, mock_start: MagicMock
):
    """No Redis client at all (connection failure) → fail OPEN, call Fly
    anyway. Worse case is one redundant (harmless) API call, never a
    silently-skipped wake."""
    from app.worker import _maybe_start_render_worker

    mock_get_redis.return_value = None
    _maybe_start_render_worker()
    mock_start.assert_called_once()


@patch("app.services.fly_machines.start_render_worker")
@patch("app.worker._get_wake_redis")
def test_redis_set_raises_fails_open_and_calls_start(
    mock_get_redis: MagicMock, mock_start: MagicMock
):
    """Redis client exists but the SET call itself raises → fail OPEN."""
    from app.worker import _maybe_start_render_worker

    mock_get_redis.return_value = _redis_client(set_side_effect=ConnectionError("blip"))
    _maybe_start_render_worker()
    mock_start.assert_called_once()


@patch("app.services.fly_machines.start_render_worker")
@patch("app.worker._get_wake_redis")
def test_debounce_key_uses_configured_ttl(mock_get_redis: MagicMock, mock_start: MagicMock):
    from app.worker import _RENDER_WAKE_DEBOUNCE_KEY, _maybe_start_render_worker

    client = _redis_client(set_return=True)
    mock_get_redis.return_value = client
    with patch("app.worker.settings") as mock_settings:
        mock_settings.RENDER_WAKE_DEBOUNCE_S = 60
        _maybe_start_render_worker()

    client.set.assert_called_once_with(_RENDER_WAKE_DEBOUNCE_KEY, "1", nx=True, ex=60)
