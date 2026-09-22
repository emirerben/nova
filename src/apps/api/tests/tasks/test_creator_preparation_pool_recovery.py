"""Recover an unclaimed pool message before its waiting creator turn expires."""

from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from app.tasks import creator_preparation as task


def _asset(now, **overrides):
    values = dict(
        id=uuid4(),
        status="queued",
        analysis_started_at=None,
        analysis_attempt_token="original-attempt",
        analysis_attempt_count=1,
        analysis_last_dispatched_at=now - task.POOL_REDISPATCH_AFTER,
        created_at=now - timedelta(minutes=5),
        gcs_generation="42",
        correlation_id="upload-correlation",
    )
    return SimpleNamespace(**(values | overrides))


def test_pending_pool_recovery_preserves_attempt_and_obeys_cooldown_and_batch():
    now = datetime.now(UTC)
    assets = [_asset(now) for _ in range(task.POOL_REDISPATCH_BATCH + 1)]

    dispatches = task._pending_pool_dispatches(assets, now)

    assert dispatches == [
        (str(asset.id), "original-attempt", "upload-correlation")
        for asset in assets[: task.POOL_REDISPATCH_BATCH]
    ]
    for asset in assets[: task.POOL_REDISPATCH_BATCH]:
        assert asset.analysis_last_dispatched_at == now
        assert asset.analysis_attempt_token == "original-attempt"
        assert asset.analysis_attempt_count == 1
        assert asset.status == "queued"
    assert task._pending_pool_dispatches(assets[:-1], now) == []
    assert task._pending_pool_dispatches(assets[-1:], now)
    assert task._pending_pool_dispatches(assets, now + timedelta(seconds=119)) == []
    assert task._pending_pool_dispatches(assets, now + timedelta(seconds=120)) == dispatches


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "uploaded"},
        {"status": "analyzing"},
        {"status": "ready"},
        {"status": "failed"},
        {"analysis_started_at": datetime.now(UTC)},
        {"analysis_attempt_token": None},
        {"gcs_generation": None},
        {"analysis_last_dispatched_at": datetime.now(UTC)},
        {"analysis_last_dispatched_at": None, "created_at": None},
    ],
)
def test_pending_pool_recovery_leaves_ineligible_assets_untouched(overrides):
    now = datetime.now(UTC)
    asset = _asset(now, **overrides)
    before = vars(asset).copy()

    assert task._pending_pool_dispatches([asset], now) == []
    assert vars(asset) == before


def _pending_task(monkeypatch, *, owned=True, expired=False):
    now = datetime.now(UTC)
    asset = _asset(now)
    attempt = SimpleNamespace(
        id=uuid4(),
        status="running",
        attempts=1,
        lease_until=now + timedelta(minutes=30),
        created_at=now - timedelta(minutes=11 if expired else 4),
    )
    graph = (attempt, SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), [], [asset])
    db = SimpleNamespace(commit=Mock())

    @contextmanager
    def session():
        yield db

    monkeypatch.setattr(task, "sync_session", session)
    monkeypatch.setattr(task, "_claim", lambda _: ("lease", {}, [], "owner", "session"))
    monkeypatch.setattr(task, "_locked", lambda *args, **kwargs: graph)
    monkeypatch.setattr(task, "owns_attempt", lambda *args: owned)
    monkeypatch.setattr(task, "pipeline_trace_for", lambda _: nullcontext())

    def pending(*args):
        raise task.PreparationPending()

    monkeypatch.setattr(task, "_analyze_sources", pending)
    fail = Mock()
    supersede = Mock()
    monkeypatch.setattr(task, "_record_failure", fail)
    monkeypatch.setattr(task, "_supersede", supersede)
    return attempt, asset, db, fail, supersede


@pytest.mark.parametrize("publish_fails", [False, True])
def test_pending_task_publishes_original_token_after_commit_and_recovers_broker_failure(
    monkeypatch, publish_fails
):
    from app.tasks.autoplace import analyze_pool_asset

    attempt, asset, db, fail, supersede = _pending_task(monkeypatch)
    old_dispatch = asset.analysis_last_dispatched_at
    published = []

    def publish(**kwargs):
        db.commit.assert_called_once()
        assert asset.analysis_last_dispatched_at > old_dispatch
        assert attempt.status == "queued"
        assert attempt.attempts == 0
        assert attempt.lease_until is None
        published.append(kwargs)
        if publish_fails:
            raise RuntimeError("private broker detail")
        return SimpleNamespace(id="broker-receipt")

    monkeypatch.setattr(analyze_pool_asset, "apply_async", publish)
    task.prepare_creator_clips.run(str(attempt.id))

    assert published == [
        {
            "args": [str(asset.id), False],
            "queue": task.settings.pool_asset_analysis_queue,
            "headers": {
                "pool_asset_attempt_token": "original-attempt",
                "x-correlation-id": "upload-correlation",
            },
        }
    ]
    assert asset.status == "queued"
    assert task._pending_pool_dispatches([asset], datetime.now(UTC)) == []
    fail.assert_not_called()
    supersede.assert_not_called()


@pytest.mark.parametrize(("owned", "expired"), [(False, False), (True, True)])
def test_pending_task_never_redispatches_superseded_or_expired_work(monkeypatch, owned, expired):
    from app.tasks.autoplace import analyze_pool_asset

    attempt, asset, db, fail, supersede = _pending_task(monkeypatch, owned=owned, expired=expired)
    before = vars(asset).copy()
    publish = Mock()
    monkeypatch.setattr(analyze_pool_asset, "apply_async", publish)

    task.prepare_creator_clips.run(str(attempt.id))

    publish.assert_not_called()
    assert vars(asset) == before
    assert fail.call_count == int(expired)
    assert supersede.call_count == int(not owned)
    db.commit.assert_called_once()
