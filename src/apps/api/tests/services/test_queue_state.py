"""Tests for app/services/queue_state.py.

Each function gets one happy-path test plus one degradation test. The
degradation tests are load-bearing: a wrongly-defaulted "unknown" → UI
that lies and lets an operator cancel a healthy job.
"""

from __future__ import annotations

import base64
import json
import uuid
from unittest.mock import MagicMock

from app.services.queue_state import (
    RENDER_WORKER_QUEUES,
    get_job_runtime_state,
    get_live_job_index,
    get_queue_position,
    get_queue_snapshot,
    render_worker_idle,
)


def _fake_celery(active=None, reserved=None, ping=None, active_queues=None, redis=None):
    """Build a minimal Celery-like object exposing .control.inspect() + connection."""
    inspector = MagicMock()
    inspector.active.return_value = active
    inspector.reserved.return_value = reserved
    inspector.ping.return_value = ping
    inspector.active_queues.return_value = active_queues

    control = MagicMock()
    control.inspect.return_value = inspector

    conn_ctx = MagicMock()
    conn_ctx.__enter__ = MagicMock(return_value=MagicMock(default_channel=MagicMock(client=redis)))
    conn_ctx.__exit__ = MagicMock(return_value=False)

    celery_app = MagicMock()
    celery_app.control = control
    celery_app.connection_or_acquire.return_value = conn_ctx
    return celery_app


def _broker_msg_for(job_id: str) -> bytes:
    """Build a Redis-broker Celery message envelope whose args[0] == job_id."""
    embed = {"callbacks": None, "errbacks": None, "chain": None, "chord": None}
    body = json.dumps([[job_id], {}, embed])
    envelope = {"body": base64.b64encode(body.encode()).decode()}
    return json.dumps(envelope).encode()


# ── get_live_job_index ──────────────────────────────────────────────────────


def test_live_job_index_separates_active_and_reserved() -> None:
    job_a, job_b = str(uuid.uuid4()), str(uuid.uuid4())
    celery_app = _fake_celery(
        active={"celery@worker-1": [{"args": [job_a]}]},
        reserved={"celery@worker-1": [{"args": [job_b]}]},
        ping={"celery@worker-1": {"ok": "pong"}},
    )

    index = get_live_job_index(celery_app)

    assert index.ok is True
    assert index.active == {job_a: "celery@worker-1"}
    assert index.reserved == {job_b: "celery@worker-1"}
    assert index.workers == ["celery@worker-1"]
    assert index.all_job_ids() == {job_a, job_b}


def test_live_job_index_returns_not_ok_when_inspect_raises() -> None:
    """Broker hiccup → ok=False so callers don't read empty dicts as 'all dead'."""
    celery_app = MagicMock()
    celery_app.control.inspect.side_effect = RuntimeError("broker unreachable")

    index = get_live_job_index(celery_app)

    assert index.ok is False
    assert index.all_job_ids() == set()


# ── get_job_runtime_state ───────────────────────────────────────────────────


def test_runtime_state_active() -> None:
    job_id = str(uuid.uuid4())
    celery_app = _fake_celery(
        active={"celery@worker-2": [{"args": [job_id]}]},
        reserved={},
        ping={"celery@worker-2": {"ok": "pong"}},
    )

    state = get_job_runtime_state(celery_app, job_id, celery_task_id=job_id)

    assert state.state == "active"
    assert state.worker == "celery@worker-2"
    assert state.task_id == job_id


def test_runtime_state_reserved() -> None:
    job_id = str(uuid.uuid4())
    celery_app = _fake_celery(
        active={},
        reserved={"celery@worker-3": [{"args": [job_id]}]},
        ping={"celery@worker-3": {"ok": "pong"}},
    )

    state = get_job_runtime_state(celery_app, job_id, celery_task_id=job_id)

    assert state.state == "reserved"
    assert state.worker == "celery@worker-3"


def test_runtime_state_not_found_when_inspect_ok_but_job_absent() -> None:
    """Worker is alive, job_id is gone. Smoking gun for 'worker died mid-task'."""
    job_id = str(uuid.uuid4())
    celery_app = _fake_celery(
        active={},
        reserved={},
        ping={"celery@worker-1": {"ok": "pong"}},
    )

    state = get_job_runtime_state(celery_app, job_id, celery_task_id=job_id)

    assert state.state == "not_found"
    assert state.worker is None


def test_runtime_state_unknown_when_broker_down() -> None:
    """Critical: unknown must NOT be conflated with not_found."""
    job_id = str(uuid.uuid4())
    celery_app = MagicMock()
    celery_app.control.inspect.side_effect = RuntimeError("broker unreachable")

    state = get_job_runtime_state(celery_app, job_id, celery_task_id=job_id)

    assert state.state == "unknown"


def test_runtime_state_falls_back_to_job_id_when_celery_task_id_null() -> None:
    """Legacy rows (pre-0027) have celery_task_id=None. Match by args[0] instead."""
    job_id = str(uuid.uuid4())
    celery_app = _fake_celery(
        active={"celery@worker-1": [{"args": [job_id]}]},
        reserved={},
        ping={"celery@worker-1": {"ok": "pong"}},
    )

    state = get_job_runtime_state(celery_app, job_id, celery_task_id=None)

    assert state.state == "active"
    assert state.task_id == job_id  # fell back to job_id


# ── get_queue_snapshot ──────────────────────────────────────────────────────


def test_queue_snapshot_returns_depth_and_oldest() -> None:
    job_id = str(uuid.uuid4())
    redis = MagicMock()
    redis.llen.return_value = 3
    redis.lrange.return_value = [_broker_msg_for(job_id), b"unparseable", b"also-bad"]

    celery_app = _fake_celery(
        active={},
        reserved={},
        ping={"celery@w1": {"ok": "pong"}},
        active_queues={"celery@w1": [{"name": "celery"}]},
        redis=redis,
    )

    snapshot = get_queue_snapshot(celery_app)

    assert snapshot.ok is True
    assert snapshot.active_workers == ["celery@w1"]
    assert len(snapshot.queues) == 1
    q = snapshot.queues[0]
    assert q.name == "celery"
    assert q.depth == 3
    assert q.oldest_pending_job_id == job_id


def test_queue_snapshot_marks_not_ok_when_inspect_fails() -> None:
    celery_app = MagicMock()
    celery_app.control.inspect.side_effect = RuntimeError("broker dead")

    snapshot = get_queue_snapshot(celery_app)

    assert snapshot.ok is False
    assert snapshot.queues == []


def test_queue_snapshot_tolerates_unparseable_oldest_message() -> None:
    """Decoder failure → oldest_pending_job_id=None, depth still correct."""
    redis = MagicMock()
    redis.llen.return_value = 2
    redis.lrange.return_value = [b"not-json", b"also-not-json"]

    celery_app = _fake_celery(
        active={},
        reserved={},
        ping={"celery@w1": {"ok": "pong"}},
        active_queues={"celery@w1": [{"name": "celery"}]},
        redis=redis,
    )

    snapshot = get_queue_snapshot(celery_app)

    assert snapshot.ok is True
    q = snapshot.queues[0]
    assert q.depth == 2
    assert q.oldest_pending_job_id is None


# ── get_queue_position ──────────────────────────────────────────────────────


def test_queue_position_returns_index() -> None:
    job_a, job_b, job_c = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    redis = MagicMock()
    redis.lrange.return_value = [
        _broker_msg_for(job_a),
        _broker_msg_for(job_b),
        _broker_msg_for(job_c),
    ]

    celery_app = _fake_celery(redis=redis)

    assert get_queue_position(celery_app, job_a) == 0
    assert get_queue_position(celery_app, job_b) == 1
    assert get_queue_position(celery_app, job_c) == 2


def test_queue_position_returns_none_when_absent() -> None:
    redis = MagicMock()
    redis.lrange.return_value = [_broker_msg_for(str(uuid.uuid4()))]

    celery_app = _fake_celery(redis=redis)

    missing = str(uuid.uuid4())
    assert get_queue_position(celery_app, missing) is None


def test_queue_position_returns_none_on_redis_failure() -> None:
    redis = MagicMock()
    redis.lrange.side_effect = RuntimeError("redis down")

    celery_app = _fake_celery(redis=redis)

    assert get_queue_position(celery_app, str(uuid.uuid4())) is None


# ── render_worker_idle ───────────────────────────────────────────────────────


def _zero_llen_redis() -> MagicMock:
    """A redis mock whose llen() returns 0 for every queue name."""
    redis = MagicMock()
    redis.llen.side_effect = lambda name: 0
    return redis


def test_render_worker_idle_true_when_nothing_active_and_queues_empty() -> None:
    """Steady-state idle: no worker bound to a render queue, zero depth."""
    celery_app = _fake_celery(active={}, reserved={}, active_queues={}, redis=_zero_llen_redis())
    assert render_worker_idle(celery_app) is True


def test_render_worker_idle_false_when_render_worker_has_active_task() -> None:
    """A worker bound to a render-worker queue with an in-flight task → not idle."""
    celery_app = _fake_celery(
        active={"celery@worker-1": [{"args": ["job-x"]}]},
        reserved={},
        active_queues={"celery@worker-1": [{"name": "celery"}]},
        redis=_zero_llen_redis(),
    )
    assert render_worker_idle(celery_app) is False


def test_render_worker_idle_false_when_render_worker_has_reserved_task() -> None:
    celery_app = _fake_celery(
        active={},
        reserved={"celery@worker-1": [{"args": ["job-x"]}]},
        active_queues={"celery@worker-1": [{"name": "plan-jobs"}]},
        redis=_zero_llen_redis(),
    )
    assert render_worker_idle(celery_app) is False


def test_render_worker_idle_ignores_active_tasks_on_non_render_workers() -> None:
    """The light/beat processes are also live Celery consumers — their
    in-flight maintenance tasks must NOT count as "render worker busy"."""
    celery_app = _fake_celery(
        active={"celery@light-1": [{"args": ["maintenance-task"]}]},
        reserved={},
        active_queues={"celery@light-1": [{"name": "maintenance"}]},
        redis=_zero_llen_redis(),
    )
    assert render_worker_idle(celery_app) is True


def test_render_worker_idle_false_when_render_queue_has_depth() -> None:
    """The critical case: render worker machine is STOPPED (invisible to
    inspect(), so active_queues={} and active/reserved={}), but a job is
    sitting in the queue waiting to be picked up. Queue depth is the ONLY
    signal available in this state — must not report idle."""
    redis = MagicMock()
    redis.llen.side_effect = lambda name: 3 if name == "overlay-jobs" else 0

    celery_app = _fake_celery(active={}, reserved={}, active_queues={}, redis=redis)
    assert render_worker_idle(celery_app) is False


def test_render_worker_idle_none_on_inspect_failure() -> None:
    """Broker hiccup → None, never True. Callers must treat None as 'not
    idle' — a stop decision made on missing information could stop a
    machine mid-render."""
    celery_app = _fake_celery(redis=_zero_llen_redis())
    celery_app.control.inspect.side_effect = ConnectionError("redis down")
    assert render_worker_idle(celery_app) is None


def test_render_worker_idle_none_on_redis_llen_failure() -> None:
    """inspect() succeeds but the Redis LLEN pass fails → unknown, not idle."""
    redis = MagicMock()
    redis.llen.side_effect = RuntimeError("redis down")
    celery_app = _fake_celery(active={}, reserved={}, active_queues={}, redis=redis)
    assert render_worker_idle(celery_app) is None


def test_render_worker_queues_constant_matches_fly_toml_worker_queues() -> None:
    """Pin the constant's contents — this must stay in sync with fly.toml's
    `celery ... -Q celery,plan-jobs,overlay-jobs` by hand (no way to share
    a literal between TOML and Python). A drift here silently breaks BOTH
    the wake-hook signal filter and this idle-check."""
    assert RENDER_WORKER_QUEUES == frozenset({"celery", "plan-jobs", "overlay-jobs"})
