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


# ── start while `stopping` (prod 2026-09-23 stop/start race) ─────────────────
#
# Fly answers `start` with 412 Precondition Failed until a stopping machine
# reaches `stopped`. The start must wait that out instead of failing.


class _FlyStub:
    """Scripted Fly API: successive list responses (machine states), /wait
    responses, and start responses. Records every call for assertions."""

    def __init__(self, states, *, start_statuses=(200,), wait_status=200, instance_id="inst-1"):
        self._states = list(states)
        self._start_statuses = list(start_statuses)
        self._wait_status = wait_status
        self._instance_id = instance_id
        self.wait_calls: list[dict] = []
        self.post_calls: list[tuple[str, object]] = []

    def get(self, url, headers=None, timeout=None, params=None):
        if url.endswith("/wait"):
            self.wait_calls.append(params)
            return _resp(self._wait_status, {})
        state = self._states.pop(0) if len(self._states) > 1 else self._states[0]
        machine = _machine("m-worker", state, "worker")
        if self._instance_id:
            machine["instance_id"] = self._instance_id
        return _resp(200, [machine])

    def post(self, url, headers=None, json=None, timeout=None):
        self.post_calls.append((url, json))
        status = (
            self._start_statuses.pop(0)
            if len(self._start_statuses) > 1
            else self._start_statuses[0]
        )
        resp = _resp(status, {})
        resp.text = "precondition failed" if status == 412 else ""
        return resp


@pytest.fixture
def no_sleep():
    with patch("app.services.fly_machines.time.sleep") as mock_sleep:
        yield mock_sleep


def _install(stub: _FlyStub):
    return (
        patch("app.services.fly_machines.httpx.get", side_effect=stub.get),
        patch("app.services.fly_machines.httpx.post", side_effect=stub.post),
    )


def test_start_while_stopping_waits_for_stopped_then_starts(no_sleep):
    from app.services.fly_machines import start_render_worker

    stub = _FlyStub(["stopping", "stopped"])
    get_patch, post_patch = _install(stub)
    with get_patch, post_patch:
        assert start_render_worker(wait_s=300) is True

    assert stub.wait_calls == [{"state": "stopped", "instance_id": "inst-1", "timeout": 60}]
    assert len(stub.post_calls) == 1
    assert stub.post_calls[0][0].endswith("m-worker/start")


def test_start_412_with_stale_started_state_rewaits_and_retries(no_sleep):
    """The list can still say `started` right after a stop is accepted; the
    412 then re-resolves, sees `stopping`, waits, and retries."""
    from app.services.fly_machines import start_render_worker

    stub = _FlyStub(["started", "stopping", "stopped"], start_statuses=(412, 200))
    get_patch, post_patch = _install(stub)
    with get_patch, post_patch:
        assert start_render_worker(wait_s=300) is True

    assert len(stub.post_calls) == 2
    assert len(stub.wait_calls) == 1


def test_start_without_wait_budget_is_a_single_attempt(no_sleep):
    """wait_s=0 keeps the pre-fix single-shot behavior: one 412, then give up."""
    from app.services.fly_machines import start_render_worker

    stub = _FlyStub(["started"], start_statuses=(412,))
    get_patch, post_patch = _install(stub)
    with get_patch, post_patch:
        assert start_render_worker() is False

    assert len(stub.post_calls) == 1
    assert stub.wait_calls == []
    no_sleep.assert_not_called()


def test_start_while_stopping_without_budget_does_not_post(no_sleep):
    from app.services.fly_machines import start_render_worker

    stub = _FlyStub(["stopping"])
    get_patch, post_patch = _install(stub)
    with get_patch, post_patch:
        assert start_render_worker(wait_s=0) is False

    assert stub.post_calls == []


def test_start_gives_up_when_the_wait_budget_runs_out(no_sleep):
    from app.services.fly_machines import start_render_worker

    stub = _FlyStub(["stopping"], wait_status=408)
    clock = [0.0]
    original_get = stub.get

    def timed_get(url, **kwargs):
        response = original_get(url, **kwargs)
        if url.endswith("/wait"):
            clock[0] += kwargs["params"]["timeout"]
        return response

    stub.get = timed_get
    get_patch, post_patch = _install(stub)
    with (
        get_patch,
        post_patch,
        patch("app.services.fly_machines.time.monotonic", side_effect=lambda: clock[0]),
    ):
        assert start_render_worker(wait_s=90) is False

    assert stub.post_calls == []
    assert [c["timeout"] for c in stub.wait_calls] == [60, 30]
    assert clock[0] == 90


def test_start_non_412_error_is_not_retried(no_sleep):
    from app.services.fly_machines import start_render_worker

    stub = _FlyStub(["stopped"], start_statuses=(500,))
    get_patch, post_patch = _install(stub)
    with get_patch, post_patch:
        assert start_render_worker(wait_s=300) is False

    assert len(stub.post_calls) == 1


def test_wait_without_instance_id_falls_back_to_backoff(no_sleep):
    """/wait?state=stopped needs instance_id; without one, sleep and re-read."""
    from app.services.fly_machines import start_render_worker

    stub = _FlyStub(["stopping", "stopped"], instance_id=None)
    get_patch, post_patch = _install(stub)
    with get_patch, post_patch:
        assert start_render_worker(wait_s=300) is True

    assert stub.wait_calls == []
    no_sleep.assert_called_once()


@pytest.mark.parametrize("wait_status", [400, 500])
def test_failed_waits_back_off_and_have_an_attempt_cap(wait_status, no_sleep):
    from app.services.fly_machines import start_render_worker

    stub = _FlyStub(["stopping"], wait_status=wait_status)
    get_patch, post_patch = _install(stub)
    with get_patch, post_patch:
        assert start_render_worker(wait_s=300) is False
    assert len(stub.wait_calls) == 12
    assert no_sleep.call_count == 12
    assert all(0 < call.args[0] <= 5 for call in no_sleep.call_args_list)
    assert stub.post_calls == []


@pytest.mark.parametrize("action", ["start", "stop"])
def test_token_disappearing_after_list_still_fails_soft(action):
    from app.services import fly_machines

    stub = _FlyStub(["stopped"])
    get_patch, post_patch = _install(stub)
    with (
        get_patch,
        post_patch,
        patch.object(
            fly_machines, "_headers", side_effect=[{"Authorization": "Bearer test-token"}, None]
        ),
    ):
        fn = (
            fly_machines.start_render_worker
            if action == "start"
            else fly_machines.stop_render_worker
        )
        assert fn() is False
    assert stub.post_calls == []
