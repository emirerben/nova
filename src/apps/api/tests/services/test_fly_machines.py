"""Tests for app/services/fly_machines.py — mocked httpx, no real network.

Every public function is fail-soft (returns None/False, never raises) so
most tests assert on the return value rather than catching exceptions.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest


def _machine(machine_id: str, state: str, process_group: str | None) -> dict:
    metadata = {}
    if process_group is not None:
        metadata["fly_process_group"] = process_group
    return {"id": machine_id, "state": state, "config": {"metadata": metadata}}


def _resp(status: int, json_body: object | None = None) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    if 400 <= status:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=resp
        )
    else:
        resp.raise_for_status.side_effect = None
    if json_body is not None:
        resp.json.return_value = json_body
    return resp


@pytest.fixture(autouse=True)
def _configured_settings():
    """Every test gets a configured token/app name unless it overrides."""
    with patch("app.services.fly_machines.settings") as mock_settings:
        mock_settings.FLY_API_TOKEN = "test-token"
        mock_settings.FLY_APP_NAME = "nova-video"
        yield mock_settings


# ── missing token ────────────────────────────────────────────────────────────


def test_no_token_configured_skips_http_entirely():
    from app.services.fly_machines import get_render_worker_state, start_render_worker

    with patch("app.services.fly_machines.settings") as mock_settings:
        mock_settings.FLY_API_TOKEN = ""
        mock_settings.FLY_APP_NAME = "nova-video"
        with patch("app.services.fly_machines.httpx.get") as mock_get:
            assert get_render_worker_state() is None
            assert start_render_worker() is False
            mock_get.assert_not_called()


# ── machine resolution ───────────────────────────────────────────────────────


@patch("app.services.fly_machines.httpx.get")
def test_resolves_single_worker_machine(mock_get: MagicMock):
    from app.services.fly_machines import get_render_worker_state

    mock_get.return_value = _resp(
        200,
        [
            _machine("m-api", "started", "api"),
            _machine("m-worker", "started", "worker"),
            _machine("m-beat", "stopped", "beat"),
        ],
    )
    assert get_render_worker_state() == "started"


@patch("app.services.fly_machines.httpx.get")
def test_zero_matches_returns_none_loud_logged(mock_get: MagicMock):
    from app.services.fly_machines import get_render_worker_state

    mock_get.return_value = _resp(
        200,
        [_machine("m-api", "started", "api"), _machine("m-beat", "stopped", "beat")],
    )
    assert get_render_worker_state() is None


@patch("app.services.fly_machines.httpx.get")
def test_ambiguous_matches_refuses_to_act(mock_get: MagicMock):
    """2+ candidates for process_group='worker' — never guess which one."""
    from app.services.fly_machines import get_render_worker_state

    mock_get.return_value = _resp(
        200,
        [
            _machine("m-worker-1", "started", "worker"),
            _machine("m-worker-2", "started", "worker"),
        ],
    )
    assert get_render_worker_state() is None


@patch("app.services.fly_machines.httpx.get")
def test_machine_missing_metadata_key_does_not_crash(mock_get: MagicMock):
    """A machine dict with no `metadata` key at all (defensive) is skipped, not fatal."""
    from app.services.fly_machines import get_render_worker_state

    mock_get.return_value = _resp(200, [{"id": "m-x", "state": "started", "config": {}}])
    assert get_render_worker_state() is None


@patch("app.services.fly_machines.httpx.get")
def test_unexpected_response_shape_treated_as_failure(mock_get: MagicMock):
    """A non-list JSON body (e.g. an error object) must not crash the resolver."""
    from app.services.fly_machines import get_render_worker_state

    mock_get.return_value = _resp(200, {"error": "not found"})
    assert get_render_worker_state() is None


# ── HTTP failure modes ───────────────────────────────────────────────────────


@patch("app.services.fly_machines.httpx.get")
def test_list_call_timeout_returns_none(mock_get: MagicMock):
    from app.services.fly_machines import get_render_worker_state

    mock_get.side_effect = httpx.TimeoutException("timed out")
    assert get_render_worker_state() is None


@patch("app.services.fly_machines.httpx.get")
def test_list_call_5xx_returns_none(mock_get: MagicMock):
    from app.services.fly_machines import get_render_worker_state

    mock_get.return_value = _resp(500)
    assert get_render_worker_state() is None


@patch("app.services.fly_machines.httpx.get")
def test_list_call_401_returns_none(mock_get: MagicMock):
    """Misconfigured/expired FLY_API_TOKEN — fails the same way as any other error."""
    from app.services.fly_machines import get_render_worker_state

    mock_get.return_value = _resp(401)
    assert get_render_worker_state() is None


# ── start / stop ─────────────────────────────────────────────────────────────


@patch("app.services.fly_machines.httpx.post")
@patch("app.services.fly_machines.httpx.get")
def test_start_render_worker_success(mock_get: MagicMock, mock_post: MagicMock):
    from app.services.fly_machines import start_render_worker

    mock_get.return_value = _resp(200, [_machine("m-worker", "stopped", "worker")])
    mock_post.return_value = _resp(200, {"id": "m-worker", "state": "starting"})

    assert start_render_worker() is True
    called_url = mock_post.call_args[0][0]
    assert "m-worker/start" in called_url


@patch("app.services.fly_machines.httpx.post")
@patch("app.services.fly_machines.httpx.get")
def test_stop_render_worker_success(mock_get: MagicMock, mock_post: MagicMock):
    from app.services.fly_machines import stop_render_worker

    mock_get.return_value = _resp(200, [_machine("m-worker", "started", "worker")])
    mock_post.return_value = _resp(200, {"id": "m-worker", "state": "stopping"})

    assert stop_render_worker() is True
    called_url = mock_post.call_args[0][0]
    assert "m-worker/stop" in called_url


@patch("app.services.fly_machines.httpx.post")
@patch("app.services.fly_machines.httpx.get")
def test_start_render_worker_post_failure_returns_false(mock_get: MagicMock, mock_post: MagicMock):
    from app.services.fly_machines import start_render_worker

    mock_get.return_value = _resp(200, [_machine("m-worker", "stopped", "worker")])
    mock_post.side_effect = httpx.ConnectError("connection refused")

    assert start_render_worker() is False


@patch("app.services.fly_machines.httpx.post")
@patch("app.services.fly_machines.httpx.get")
def test_start_render_worker_ambiguous_never_calls_post(mock_get: MagicMock, mock_post: MagicMock):
    """Ambiguous resolution must short-circuit before any start/stop POST fires."""
    from app.services.fly_machines import start_render_worker

    mock_get.return_value = _resp(
        200,
        [
            _machine("m-worker-1", "started", "worker"),
            _machine("m-worker-2", "started", "worker"),
        ],
    )

    assert start_render_worker() is False
    mock_post.assert_not_called()


@patch("app.services.fly_machines.httpx.post")
@patch("app.services.fly_machines.httpx.get")
def test_stop_render_worker_zero_match_never_calls_post(mock_get: MagicMock, mock_post: MagicMock):
    from app.services.fly_machines import stop_render_worker

    mock_get.return_value = _resp(200, [_machine("m-api", "started", "api")])

    assert stop_render_worker() is False
    mock_post.assert_not_called()
