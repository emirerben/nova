"""Tests for app.services.build_task_repo — the build_task queue's repository.

These exercise the REAL SQL against a real Postgres (the SKIP LOCKED claim,
status transitions, and concurrency cannot be faithfully mocked — a MagicMock
session can't reproduce row locking). The whole module skips when Postgres is
unreachable so it stays runnable offline; the CRITICAL atomic-claim concurrency
test additionally needs two real connections, so it's the one to flag for a
machine without a live DB.

Covers the D3 resilience suite at the repo level:
  - atomic-claim (two concurrent claims → exactly one wins, via SKIP LOCKED)
  - resume-after-kill (checkpoint persists; a fresh claim continues, not restart)
  - soft-exit-on-limit (release → queued, NOT failed, attempt_count untouched)
  - idempotency (a done task is never re-claimed)
  - attempt-cap (N fails → blocked, no infinite loop)
  - security invariant (untrusted provenance can never mint a task)
"""

from __future__ import annotations

import os
import threading

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.models import Base, BuildTask
from app.services import build_task_repo

# ── Real-Postgres fixture (skip cleanly when unavailable) ────────────────────

_DB_URL = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/nova_test"
).replace("postgresql+asyncpg://", "postgresql://")


def _engine_or_skip():
    try:
        eng = create_engine(_DB_URL, pool_pre_ping=True)
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        return eng
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres not reachable for build_task_repo tests: {exc!r}")


@pytest.fixture(scope="module")
def engine():
    eng = _engine_or_skip()
    # Ensure just the build_task table exists (idempotent — migrations may have
    # already created it). create_all only touches missing tables.
    BuildTask.__table__.create(bind=eng, checkfirst=True)
    return eng


@pytest.fixture()
def db(engine):
    """A clean session with build_task truncated before each test."""
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE build_task"))
    session = Session(engine, expire_on_commit=False)
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _mint(db, *, title="t", priority=100, provenance="trusted"):
    task = build_task_repo.create_build_task(
        db, title=title, priority=priority, provenance=provenance
    )
    db.commit()
    return task


# ── Minting + security invariant ─────────────────────────────────────────────


class TestSecurityInvariant:
    def test_trusted_provenance_mints(self, db):
        task = build_task_repo.create_build_task(db, title="rubric gap", provenance="trusted")
        db.commit()
        assert task.id is not None
        assert task.status == "queued"
        assert task.provenance == "trusted"

    def test_untrusted_provenance_is_rejected(self, db):
        """CEO D3: an untrusted signal must NEVER auto-mint a build_task in v1."""
        with pytest.raises(build_task_repo.UntrustedProvenanceError):
            build_task_repo.create_build_task(
                db, title="from a VideoFeedback note", provenance="untrusted"
            )
        db.rollback()
        # And nothing was written.
        assert db.query(BuildTask).count() == 0

    def test_unknown_provenance_is_rejected(self, db):
        with pytest.raises(ValueError):
            build_task_repo.create_build_task(db, title="x", provenance="reddit")
        db.rollback()

    def test_mintable_set_is_trusted_only(self):
        """Pin the invariant at the model layer so widening it is a conscious diff."""
        assert BuildTask.MINTABLE_PROVENANCES == ("trusted",)
        assert "untrusted" not in BuildTask.MINTABLE_PROVENANCES


# ── Claim ordering + idempotency ─────────────────────────────────────────────


class TestClaim:
    def test_claim_returns_none_on_empty_queue(self, db):
        assert build_task_repo.claim_next_task(db) is None

    def test_claim_flips_to_in_progress_and_stamps(self, db):
        _mint(db, title="only")
        claimed = build_task_repo.claim_next_task(db, claimed_by="run-1")
        db.commit()
        assert claimed is not None
        assert claimed.status == "in_progress"
        assert claimed.claimed_by == "run-1"
        assert claimed.claimed_at is not None

    def test_claim_respects_priority_then_created_at(self, db):
        _mint(db, title="low-prio", priority=200)
        _mint(db, title="high-prio", priority=10)
        claimed = build_task_repo.claim_next_task(db)
        db.commit()
        assert claimed.title == "high-prio"

    def test_done_task_is_never_re_claimed(self, db):
        """Idempotency: a completed task is skipped on any future claim."""
        t = _mint(db, title="done-one")
        build_task_repo.complete_task(db, t.id)
        db.commit()
        # Queue now has zero queued rows.
        assert build_task_repo.claim_next_task(db) is None

    def test_blocked_task_is_not_claimed(self, db):
        t = _mint(db, title="blocked-one")
        build_task_repo.block_task(db, t.id)
        db.commit()
        assert build_task_repo.claim_next_task(db) is None

    def test_in_progress_task_is_not_re_claimed(self, db):
        """A second claim on a single in-progress task gets nothing (no double-grab)."""
        _mint(db, title="one")
        first = build_task_repo.claim_next_task(db, claimed_by="run-1")
        db.commit()
        assert first is not None
        second = build_task_repo.claim_next_task(db, claimed_by="run-2")
        db.commit()
        assert second is None


# ── Resume-after-kill ─────────────────────────────────────────────────────────


class TestResumeAfterKill:
    def test_checkpoint_persists_and_resumed_claim_continues(self, db):
        """Interrupt mid-task → the next claim continues from the checkpoint.

        Simulates a runner dying after a checkpoint: the task is reset to queued
        (by the reaper) WITHOUT losing stage/branch/progress_note, so a fresh
        claim re-orients from the persisted state instead of restarting.
        """
        t = _mint(db, title="resumable")
        build_task_repo.claim_next_task(db, claimed_by="run-1")
        db.commit()
        build_task_repo.checkpoint_task(
            db,
            t.id,
            stage="stage-E",
            progress_note="aligned 3/5 overlays",
            branch="builder/abc123",
        )
        db.commit()

        # Runner dies → reaper resets it to queued (checkpoint MUST survive).
        task = build_task_repo.get_task(db, t.id)
        build_task_repo.reap_stale_task(db, task)
        db.commit()

        resumed = build_task_repo.claim_next_task(db, claimed_by="run-2")
        db.commit()
        assert resumed.id == t.id
        assert resumed.stage == "stage-E"
        assert resumed.progress_note == "aligned 3/5 overlays"
        assert resumed.branch == "builder/abc123"  # not a fresh restart
        assert resumed.status == "in_progress"


# ── Soft-exit-on-limit ────────────────────────────────────────────────────────


class TestSoftExitOnLimit:
    def test_release_returns_to_queued_without_bumping_attempt(self, db):
        """A usage limit is NOT a failure: release → queued, attempt untouched."""
        t = _mint(db, title="limited")
        build_task_repo.claim_next_task(db, claimed_by="run-1")
        db.commit()
        before = build_task_repo.get_task(db, t.id).attempt_count

        build_task_repo.release_task(db, t.id, progress_note="paused on usage limit 11:10")
        db.commit()

        after = build_task_repo.get_task(db, t.id)
        assert after.status == "queued"  # resumable, NOT failed/blocked
        assert after.attempt_count == before  # NOT bumped
        assert after.claimed_at is None
        assert after.progress_note == "paused on usage limit 11:10"
        # And it is immediately re-claimable.
        assert build_task_repo.claim_next_task(db).id == t.id

    def test_release_is_distinct_from_fail(self, db):
        """fail() bumps attempt_count; release() does not — the whole point."""
        t = _mint(db, title="x")
        build_task_repo.claim_next_task(db)
        db.commit()
        build_task_repo.fail_task(db, t.id)
        db.commit()
        assert build_task_repo.get_task(db, t.id).attempt_count == 1


# ── Attempt cap ───────────────────────────────────────────────────────────────


class TestAttemptCap:
    def test_repeated_failures_eventually_block(self, db):
        """N failures → blocked, never an infinite retry loop."""
        t = _mint(db, title="flaky")
        cap = build_task_repo.ATTEMPT_CAP
        last = None
        for _ in range(cap):
            # claim → fail, each round
            claimed = build_task_repo.claim_next_task(db)
            db.commit()
            if claimed is None:
                # Already blocked before exhausting the loop.
                break
            last = build_task_repo.fail_task(db, t.id)
            db.commit()
        final = build_task_repo.get_task(db, t.id)
        assert final.status == "blocked"
        assert final.attempt_count >= cap
        # A blocked task is no longer claimable → loop terminates.
        assert build_task_repo.claim_next_task(db) is None
        assert last is not None

    def test_below_cap_requeues(self, db):
        t = _mint(db, title="retry-once")
        build_task_repo.claim_next_task(db)
        db.commit()
        build_task_repo.fail_task(db, t.id)
        db.commit()
        after = build_task_repo.get_task(db, t.id)
        # 1 < cap → still queued (retryable)
        assert after.status == "queued"
        assert after.attempt_count == 1

    def test_reset_unblocks_and_clears_attempts(self, db):
        t = _mint(db, title="unblock-me")
        build_task_repo.block_task(db, t.id)
        db.commit()
        build_task_repo.reset_task(db, t.id)
        db.commit()
        after = build_task_repo.get_task(db, t.id)
        assert after.status == "queued"
        assert after.attempt_count == 0


# ── Atomic-claim concurrency (CRITICAL — needs a real DB + two connections) ───


class TestAtomicClaimConcurrency:
    def test_two_concurrent_claims_exactly_one_wins(self, engine):
        """SKIP LOCKED: two simultaneous claimers on a SINGLE queued task →
        exactly one gets it, the other gets None. No double-claim, ever.

        This is the load-bearing concurrency guarantee. It requires two real
        DB connections racing on the same row — a mock can't reproduce it.
        """
        # Clean slate + one queued task.
        with engine.begin() as conn:
            conn.execute(text("TRUNCATE TABLE build_task"))
        with Session(engine, expire_on_commit=False) as setup:
            build_task_repo.create_build_task(setup, title="contended", provenance="trusted")
            setup.commit()

        results: list[BuildTask | None] = []
        barrier = threading.Barrier(2)
        errors: list[Exception] = []

        def claim(run_id: str):
            try:
                with Session(engine, expire_on_commit=False) as s:
                    barrier.wait(timeout=5)  # line both threads up on the lock
                    task = build_task_repo.claim_next_task(s, claimed_by=run_id)
                    # Hold the transaction open briefly so the lock overlaps the
                    # other thread's attempt (SKIP LOCKED makes the loser skip it).
                    if task is not None:
                        s.execute(text("SELECT pg_sleep(0.2)"))
                    s.commit()
                    results.append(task)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        t1 = threading.Thread(target=claim, args=("run-1",))
        t2 = threading.Thread(target=claim, args=("run-2",))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert not errors, f"claim raised: {errors!r}"
        winners = [r for r in results if r is not None]
        losers = [r for r in results if r is None]
        assert len(winners) == 1, f"expected exactly one winner, got {len(winners)}"
        assert len(losers) == 1, f"expected exactly one loser (None), got {len(losers)}"

        # And the DB shows exactly one in_progress row, claimed by the winner.
        with Session(engine, expire_on_commit=False) as check:
            rows = check.query(BuildTask).all()
            assert len(rows) == 1
            assert rows[0].status == "in_progress"
            assert rows[0].claimed_by in {"run-1", "run-2"}


# keep Base referenced so the import isn't flagged unused (table registration).
_ = Base
