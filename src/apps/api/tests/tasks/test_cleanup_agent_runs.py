"""Tests for tasks.maintenance.cleanup_agent_runs.

Validates:
  - cutoff is derived from retention_days (default = settings)
  - the loop short-circuits when a batch returns fewer rows than the limit
  - the loop is hard-capped by _AGENT_RUN_DELETE_MAX_BATCHES
  - the DELETE statement only targets job_id IS NOT NULL rows (template- /
    track-scoped runs survive)

The task runs synchronous SQL through ``app.database.sync_engine``. We don't
spin up a real Postgres here — we mock the engine's connection and assert
the queries it executes have the right shape and parameters.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import app.database as database_mod
from app.tasks.maintenance import (
    _AGENT_RUN_DELETE_BATCH,
    _AGENT_RUN_DELETE_MAX_BATCHES,
    cleanup_agent_runs,
)


class _FakeConn:
    """Stand-in for a SQLAlchemy Connection that records executed statements."""

    def __init__(self, rowcounts: list[int]):
        # rowcounts is consumed left-to-right per execute() call.
        self._rowcounts = list(rowcounts)
        self.calls: list[tuple[str, dict]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, stmt, params):
        # str(text("...")) yields the SQL fragment as written.
        self.calls.append((str(stmt), dict(params)))
        rowcount = self._rowcounts.pop(0) if self._rowcounts else 0
        res = MagicMock()
        res.rowcount = rowcount
        return res


def _patch_engine(rowcounts: list[int]) -> _FakeConn:
    conn = _FakeConn(rowcounts)

    fake_engine = MagicMock()
    # sync_engine.begin() returns a context manager yielding the conn.
    fake_engine.begin.return_value = conn

    # The task does ``from app.database import sync_engine`` at call time,
    # so the patch has to target the source module, not the task module.
    patcher = patch.object(database_mod, "sync_engine", fake_engine)
    patcher.start()
    return conn


def test_cleanup_short_circuits_on_partial_batch():
    """If the first DELETE returns fewer rows than the batch limit, no
    second DELETE should fire — there's nothing left to prune."""
    conn = _patch_engine(rowcounts=[3])
    try:
        result = cleanup_agent_runs(retention_days=30)
    finally:
        patch.stopall()

    assert result["deleted"] == 3
    assert result["batches"] == 1
    assert len(conn.calls) == 1


def test_cleanup_keeps_looping_until_partial_batch_or_cap():
    """Full batches must drive another iteration; a final short batch ends it."""
    conn = _patch_engine(
        rowcounts=[
            _AGENT_RUN_DELETE_BATCH,
            _AGENT_RUN_DELETE_BATCH,
            42,
        ]
    )
    try:
        result = cleanup_agent_runs(retention_days=7)
    finally:
        patch.stopall()

    assert result["batches"] == 3
    assert result["deleted"] == 2 * _AGENT_RUN_DELETE_BATCH + 42
    assert len(conn.calls) == 3


def test_cleanup_respects_max_batches_safety_cap():
    """Even with infinite full batches the loop must stop at the cap.
    Provide more than the cap; only `_AGENT_RUN_DELETE_MAX_BATCHES` should run."""
    conn = _patch_engine(rowcounts=[_AGENT_RUN_DELETE_BATCH] * (_AGENT_RUN_DELETE_MAX_BATCHES + 5))
    try:
        result = cleanup_agent_runs(retention_days=30)
    finally:
        patch.stopall()

    assert result["batches"] == _AGENT_RUN_DELETE_MAX_BATCHES
    assert len(conn.calls) == _AGENT_RUN_DELETE_MAX_BATCHES


def test_cleanup_delete_filters_to_job_scoped_rows():
    """Track- and template-scoped agent_run rows (job_id IS NULL) must NEVER
    be touched by this task — they back per-template debug views and are
    not growth-bound to job volume."""
    conn = _patch_engine(rowcounts=[0])
    try:
        cleanup_agent_runs(retention_days=30)
    finally:
        patch.stopall()

    assert len(conn.calls) == 1
    sql, params = conn.calls[0]
    assert "delete from agent_run" in sql.lower()
    assert "job_id is not null" in sql.lower()
    assert "created_at < :cutoff" in sql.lower()
    assert params["batch"] == _AGENT_RUN_DELETE_BATCH


def test_cleanup_cutoff_is_retention_days_in_the_past():
    conn = _patch_engine(rowcounts=[0])
    before = datetime.now(UTC)
    try:
        result = cleanup_agent_runs(retention_days=15)
    finally:
        patch.stopall()
    after = datetime.now(UTC)

    cutoff = datetime.fromisoformat(result["cutoff"])
    # cutoff should be exactly retention_days in the past, within the
    # before/after window of the test invocation.
    assert (before - timedelta(days=15)) <= cutoff <= (after - timedelta(days=15))

    # And the parameter passed to the DELETE matches.
    _, params = conn.calls[0]
    assert params["cutoff"] == cutoff


def test_cleanup_default_retention_comes_from_settings():
    """When retention_days is None, the task reads settings.agent_run_retention_days."""
    conn = _patch_engine(rowcounts=[0])
    fake_settings = MagicMock()
    fake_settings.agent_run_retention_days = 7
    try:
        with patch("app.config.settings", fake_settings):
            result = cleanup_agent_runs(retention_days=None)
    finally:
        patch.stopall()

    cutoff = datetime.fromisoformat(result["cutoff"])
    expected = datetime.now(UTC) - timedelta(days=7)
    # Allow a small wall-clock skew between the call and the assertion.
    assert abs((cutoff - expected).total_seconds()) < 5
    assert len(conn.calls) == 1
