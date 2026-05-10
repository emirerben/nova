"""Unit tests for app.tasks.reaper.

Mocks `sync_session` (DB) and `celery_app.control.inspect()` (broker)
so the suite runs without Postgres/Redis.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _make_celery_with_inspect(active=None, reserved=None, raises=None):
    """Build a fake Celery app whose .control.inspect() returns the given dicts.

    Pass `active={"worker1": [{"args": ["job-id-1"]}]}` to simulate a live task.
    Pass `raises=Exception("boom")` to simulate broker failure.
    """
    app = MagicMock()
    inspector = MagicMock()
    if raises is not None:
        inspector.active.side_effect = raises
        inspector.reserved.side_effect = raises
    else:
        inspector.active.return_value = active or {}
        inspector.reserved.return_value = reserved or {}
    app.control.inspect.return_value = inspector
    return app


def _patch_sync_session(rowcount: int = 0):
    """Returns a patch context for sync_session that yields a fake session
    whose db.execute(...) returns a result with the given rowcount."""
    session = MagicMock()
    result = MagicMock()
    result.rowcount = rowcount
    session.execute.return_value = result

    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=session)
    ctx.__exit__ = MagicMock(return_value=False)

    return patch("app.tasks.reaper.sync_session", return_value=ctx), session


class TestLiveJobIds:
    def test_returns_empty_set_when_no_active_or_reserved(self):
        from app.tasks.reaper import _live_job_ids
        app = _make_celery_with_inspect(active={}, reserved={})
        assert _live_job_ids(app) == set()

    def test_collects_first_arg_of_each_active_task(self):
        from app.tasks.reaper import _live_job_ids
        app = _make_celery_with_inspect(active={
            "celery@worker1": [{"args": ["job-aaa"]}, {"args": ["job-bbb"]}],
            "celery@worker2": [{"args": ["job-ccc"]}],
        })
        assert _live_job_ids(app) == {"job-aaa", "job-bbb", "job-ccc"}

    def test_includes_reserved_tasks(self):
        from app.tasks.reaper import _live_job_ids
        app = _make_celery_with_inspect(
            active={"w1": [{"args": ["a"]}]},
            reserved={"w1": [{"args": ["b"]}]},
        )
        assert _live_job_ids(app) == {"a", "b"}

    def test_skips_tasks_with_no_args(self):
        from app.tasks.reaper import _live_job_ids
        app = _make_celery_with_inspect(active={
            "w1": [{"args": []}, {"args": ["only-this"]}, {}],
        })
        assert _live_job_ids(app) == {"only-this"}

    def test_returns_none_on_inspect_failure(self):
        """Broker hiccup → None signals 'unknown — don't reap'."""
        from app.tasks.reaper import _live_job_ids
        app = _make_celery_with_inspect(raises=ConnectionError("redis down"))
        assert _live_job_ids(app) is None

    def test_active_returning_none_treated_as_empty(self):
        """celery_app.control.inspect().active() returns None when no workers report."""
        from app.tasks.reaper import _live_job_ids
        app = MagicMock()
        inspector = MagicMock()
        inspector.active.return_value = None
        inspector.reserved.return_value = None
        app.control.inspect.return_value = inspector
        assert _live_job_ids(app) == set()


class TestReapOrphans:
    def test_no_op_when_inspect_fails(self):
        """Inspection failure → no reap (safer to skip than false-positive)."""
        from app.tasks.reaper import reap_orphans
        app = _make_celery_with_inspect(raises=ConnectionError("redis down"))
        # sync_session should NOT even be called
        with patch("app.tasks.reaper.sync_session") as mock_session:
            assert reap_orphans(app) == 0
            mock_session.assert_not_called()

    def test_returns_rowcount_from_db(self):
        """Happy path: inspect returns nothing live, DB reaps 5 rows."""
        from app.tasks.reaper import reap_orphans
        app = _make_celery_with_inspect(active={}, reserved={})
        patch_ctx, session = _patch_sync_session(rowcount=5)
        with patch_ctx:
            assert reap_orphans(app) == 5
        # Verify it actually ran an UPDATE + commit.
        session.execute.assert_called_once()
        session.commit.assert_called_once()

    def test_zero_rowcount_returns_zero(self):
        from app.tasks.reaper import reap_orphans
        app = _make_celery_with_inspect(active={}, reserved={})
        patch_ctx, _ = _patch_sync_session(rowcount=0)
        with patch_ctx:
            assert reap_orphans(app) == 0

    def test_excludes_live_jobs_from_update(self):
        """When live jobs exist, the WHERE clause must include NOT IN(live)."""
        import uuid as _uuid

        from app.tasks.reaper import reap_orphans
        live_uuid = str(_uuid.uuid4())
        app = _make_celery_with_inspect(active={
            "w1": [{"args": [live_uuid]}],
        })
        patch_ctx, session = _patch_sync_session(rowcount=2)
        with patch_ctx:
            reap_orphans(app)
        # Inspect the SQL statement passed to execute() — verify NOT IN clause
        # exists and references the live job id (as a UUID parameter).
        stmt = session.execute.call_args[0][0]
        # Render with bind params visible (not literal — UUID type can't
        # always be literal-rendered across dialects).
        sql_str = str(stmt)
        assert "NOT IN" in sql_str.upper() or "not_in" in sql_str.lower()
        # And confirm the live UUID flows into the compiled params.
        compiled = stmt.compile()
        assert any(live_uuid in str(v) for v in compiled.params.values())

    def test_no_not_in_clause_when_live_set_empty(self):
        """Empty live set → SQL must NOT contain `NOT IN ()` (empty IN is invalid)."""
        from app.tasks.reaper import reap_orphans
        app = _make_celery_with_inspect(active={}, reserved={})
        patch_ctx, session = _patch_sync_session(rowcount=3)
        with patch_ctx:
            reap_orphans(app)
        stmt = session.execute.call_args[0][0]
        sql_str = str(stmt)
        # The two safety clauses (status IN, updated_at <) must always be present.
        assert "status" in sql_str
        assert "updated_at" in sql_str
        # No empty NOT IN — would be a SQL error.
        assert "NOT IN ()" not in sql_str.upper()
        assert "NOT IN" not in sql_str.upper()  # absent entirely when live={}

    def test_threshold_min_controls_cutoff(self):
        """Verify threshold_min flows into the updated_at< comparison."""
        from app.tasks.reaper import reap_orphans
        app = _make_celery_with_inspect(active={}, reserved={})
        patch_ctx, session = _patch_sync_session(rowcount=0)
        with patch_ctx:
            # Just smoke-test that a custom threshold doesn't crash.
            reap_orphans(app, threshold_min=5)
        # The cutoff string is dynamic (current time), so we can't pin an exact
        # value. But we can confirm the SQL was compiled and ran.
        session.execute.assert_called_once()

    def test_writes_processing_failed_with_unknown_failure_reason(self):
        """The reaped row gets the right marker fields."""
        from app.tasks.reaper import reap_orphans
        app = _make_celery_with_inspect(active={}, reserved={})
        patch_ctx, session = _patch_sync_session(rowcount=1)
        with patch_ctx:
            reap_orphans(app)
        stmt = session.execute.call_args[0][0]
        # Values land in compiled bind params, not the SQL text.
        params = stmt.compile().params
        values = {str(v) for v in params.values()}
        assert "processing_failed" in values
        assert "unknown" in values
        assert any("Resubmit" in str(v) for v in values)  # user-facing error_detail


class TestThresholdConstant:
    """The 60-min threshold is load-bearing for the no-false-positive guarantee."""

    def test_threshold_is_2x_hard_time_limit(self):
        """Threshold (min) must be ≥ 2× orchestrate_template_job hard time_limit
        (1800s = 30min) so a legitimately slow finisher always wins the race."""
        from app.tasks.reaper import THRESHOLD_MIN
        assert THRESHOLD_MIN >= 60, (
            f"THRESHOLD_MIN={THRESHOLD_MIN} too low — must be 2× the multi-clip "
            f"hard time_limit (1800s/60min) to avoid reaping legit slow jobs."
        )


@pytest.mark.parametrize("status,should_reap", [
    ("processing", True),
    ("template_ready", True),
    ("completed", False),
    ("processing_failed", False),
    ("queued", False),
])
def test_non_terminal_statuses_constant_includes_correct_set(status, should_reap):
    """Sanity-pin the status filter so a future schema change is caught."""
    from app.tasks.reaper import _NON_TERMINAL_STATUSES
    assert (status in _NON_TERMINAL_STATUSES) is should_reap
