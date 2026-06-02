"""Tests for app.tasks.build_task_reaper — the stale-build_task sweep.

Two layers:
  - Unit (mock sync_session) so the sweep's bookkeeping runs offline, mirroring
    tests/tasks/test_reaper.py's discipline.
  - Real-Postgres (skips when unavailable) for the end-to-end "stale row past
    threshold → reset to queued; cap tripped → blocked" behavior, which depends
    on the actual timestamp comparison + repo transitions.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.models import BuildTask

_DB_URL = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/nova_test"
).replace("postgresql+asyncpg://", "postgresql://")


# ── Unit: constants + offline bookkeeping ─────────────────────────────────────


class TestReaperConstants:
    def test_threshold_is_generous(self):
        """The reaper threshold must comfortably exceed one timeout-bounded run.

        A builder tick is ~15 min; the reaper must NOT reap a live, mid-chunk
        task. STALE_THRESHOLD_MIN well above 15 guarantees the temporal
        cross-check ("a live run re-stamps claimed_at every tick") holds.
        """
        from app.services.build_task_repo import STALE_THRESHOLD_MIN

        assert STALE_THRESHOLD_MIN >= 30, (
            "STALE_THRESHOLD_MIN too tight — could reap a live builder chunk."
        )

    def test_empty_sweep_returns_zero(self):
        """No stale rows → no-op summary, single short transaction."""
        from app.tasks.build_task_reaper import reap_stale_build_tasks

        session = MagicMock()
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=session)
        ctx.__exit__ = MagicMock(return_value=False)
        with (
            patch("app.tasks.build_task_reaper.sync_session", return_value=ctx),
            patch(
                "app.tasks.build_task_reaper.build_task_repo.find_stale_in_progress",
                return_value=[],
            ),
        ):
            summary = reap_stale_build_tasks()
        assert summary == {"requeued": 0, "blocked": 0, "total": 0}
        session.commit.assert_called_once()


# ── Real-Postgres end-to-end ──────────────────────────────────────────────────


def _engine_or_skip():
    try:
        eng = create_engine(_DB_URL, pool_pre_ping=True)
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        return eng
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres not reachable for build_task_reaper tests: {exc!r}")


@pytest.fixture(scope="module")
def engine():
    eng = _engine_or_skip()
    BuildTask.__table__.create(bind=eng, checkfirst=True)
    return eng


@pytest.fixture()
def db(engine):
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE build_task"))
    session = Session(engine, expire_on_commit=False)
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _stale_in_progress(db, *, minutes_ago: int, attempt_count: int = 0) -> BuildTask:
    """Insert an in_progress row claimed `minutes_ago` minutes in the past."""
    task = BuildTask(
        title="stuck",
        status="in_progress",
        provenance="trusted",
        attempt_count=attempt_count,
        claimed_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
        claimed_by="dead-runner",
    )
    db.add(task)
    db.commit()
    return task


class TestStaleReaper:
    def test_stale_task_is_reset_to_queued(self, db, engine):
        """A row in_progress past the threshold → reset to queued (resumable)."""
        from app.services.build_task_repo import STALE_THRESHOLD_MIN
        from app.tasks.build_task_reaper import reap_stale_build_tasks

        task = _stale_in_progress(db, minutes_ago=STALE_THRESHOLD_MIN + 30)
        summary = reap_stale_build_tasks()
        assert summary["requeued"] == 1
        assert summary["blocked"] == 0

        with Session(engine, expire_on_commit=False) as check:
            row = check.get(BuildTask, task.id)
            assert row.status == "queued"
            assert row.claimed_at is None
            assert row.attempt_count == 1  # the dead run counts as an attempt

    def test_fresh_in_progress_task_is_not_reaped(self, db, engine):
        """A recently-claimed (live) task must NOT be swept out from under itself."""
        from app.tasks.build_task_reaper import reap_stale_build_tasks

        task = _stale_in_progress(db, minutes_ago=2)  # well within threshold
        summary = reap_stale_build_tasks()
        assert summary["total"] == 0
        with Session(engine, expire_on_commit=False) as check:
            assert check.get(BuildTask, task.id).status == "in_progress"

    def test_stale_task_at_cap_is_blocked_not_requeued(self, db, engine):
        """A stale row that trips attempt_cap goes blocked (no infinite resurrection)."""
        from app.services.build_task_repo import ATTEMPT_CAP, STALE_THRESHOLD_MIN
        from app.tasks.build_task_reaper import reap_stale_build_tasks

        # One more reap will push attempt_count to the cap.
        task = _stale_in_progress(
            db, minutes_ago=STALE_THRESHOLD_MIN + 30, attempt_count=ATTEMPT_CAP - 1
        )
        summary = reap_stale_build_tasks()
        assert summary["blocked"] == 1
        assert summary["requeued"] == 0
        with Session(engine, expire_on_commit=False) as check:
            row = check.get(BuildTask, task.id)
            assert row.status == "blocked"
            assert row.attempt_count == ATTEMPT_CAP

    def test_queued_task_is_never_reaped(self, db, engine):
        """Only in_progress rows are candidates — a queued task is left alone."""
        from app.tasks.build_task_reaper import reap_stale_build_tasks

        task = BuildTask(title="waiting", status="queued", provenance="trusted")
        db.add(task)
        db.commit()
        summary = reap_stale_build_tasks()
        assert summary["total"] == 0
        with Session(engine, expire_on_commit=False) as check:
            assert check.get(BuildTask, task.id).status == "queued"
