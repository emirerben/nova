"""Shared repository for `build_task` status transitions (autonomous dev loop, M4).

The single owner of ALL build_task lifecycle SQL. The GitHub Actions builder
(via the admin API), the stale-task reaper, and the heartbeat digest import
from here — no scattered SQL anywhere else (Eng Review D2). Every function
takes an explicit SQLAlchemy `Session` so callers control the transaction
boundary; none of them commit (the caller commits), EXCEPT where noted.

The load-bearing primitive is `claim_next_task`:

    SELECT ... FROM build_task
     WHERE status = 'queued'
     ORDER BY priority, created_at
     LIMIT 1
       FOR UPDATE SKIP LOCKED

This is the textbook Postgres job-queue pattern. `FOR UPDATE` locks the chosen
row; `SKIP LOCKED` makes a second concurrent claimer skip the already-locked
row and grab the next one (or return nothing) instead of blocking — so two
overlapping builder runs can NEVER claim the same task. SQLAlchemy spells it
`.with_for_update(skip_locked=True)`. This pattern exists nowhere else in the
codebase, so it lives here behind one tested function.

Resumability: the claim flips `queued → in_progress` and stamps
`claimed_at`/`claimed_by` inside the same transaction as the row lock, so the
row is unambiguously owned the instant the lock releases. A soft-exit on a
Claude usage limit must call `release_task` (back to `queued`, NOT `blocked`,
NOT a bumped attempt) so the next scheduled tick resumes from the checkpoint.
Only a genuine failure calls `fail_task` (bumps attempt_count → eventually
`blocked`).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import BuildTask

log = structlog.get_logger()

# A builder run that bumped attempt_count this many times without finishing is
# wedged (e.g. an impossible task or a recurring crash). The reaper trips it to
# `blocked` so it never loops forever (CEO/Eng "no infinite loop" requirement).
ATTEMPT_CAP = 5

# A row left `in_progress` longer than this — with no run touching it — means
# the runner died mid-task (GH Actions runner killed, OOM, etc.). The reaper
# resets it to `queued` so the schedule resumes it. Generous on purpose: a
# legitimately-long builder run (the GH Actions job itself is timeout-bounded
# to ~15 min but a task spans many runs) must never be reaped out from under an
# in-flight chunk. Mirrors reaper.py's "generous threshold" discipline.
STALE_THRESHOLD_MIN = 60


class UntrustedProvenanceError(ValueError):
    """Raised when an untrusted-provenance signal tries to mint a build_task.

    Security invariant (CEO D3): in v1 only trusted signals (rubric-gap finder,
    failing evals, founder notes) may auto-mint a task. Untrusted signals
    (VideoFeedback notes, future Reddit/TikTok comments) stay read-only until
    the provenance firewall ships (deferred). The intake approval gate is a
    security boundary, not just a direction gate.
    """


# ── Minting ──────────────────────────────────────────────────────────────────


def create_build_task(
    db: Session,
    *,
    title: str,
    body: str | None = None,
    provenance: str = "trusted",
    priority: int = 100,
) -> BuildTask:
    """Mint a new `queued` build_task. Caller commits.

    SECURITY: rejects any non-trusted provenance with `UntrustedProvenanceError`.
    This is the enforcement point for the v1 quarantine — an untrusted signal
    can never become an auto-minted, builder-claimable task. Do NOT relax this
    without the provenance firewall (deferred in the plan).
    """
    if provenance not in BuildTask.PROVENANCES:
        raise ValueError(
            f"unknown provenance {provenance!r}; expected one of {BuildTask.PROVENANCES}"
        )
    if provenance not in BuildTask.MINTABLE_PROVENANCES:
        raise UntrustedProvenanceError(
            f"provenance {provenance!r} may not mint a build_task in v1 "
            f"(only {BuildTask.MINTABLE_PROVENANCES} can). Untrusted signals are "
            f"read-only until the provenance firewall ships."
        )
    task = BuildTask(
        title=title,
        body=body,
        provenance=provenance,
        priority=priority,
        status="queued",
    )
    db.add(task)
    db.flush()  # populate task.id without committing (caller owns the txn)
    return task


# ── Claim ──────────────────────────────────────────────────────────────────────


def claim_next_task(db: Session, *, claimed_by: str | None = None) -> BuildTask | None:
    """Atomically claim the oldest incomplete (`queued`) task, or None.

    `FOR UPDATE SKIP LOCKED LIMIT 1` guarantees two concurrent runs never grab
    the same row: the second claimer skips the row the first has locked and
    takes the next queued one (or gets None). Flips the row to `in_progress` and
    stamps claimed_at/claimed_by in the SAME transaction as the lock, so the
    claim is durable the instant the lock releases. Caller commits.

    `done` and `blocked` rows are invisible to the claim (idempotency: a
    completed task is never re-handed-out; a blocked task waits for a human).
    `in_progress` rows are also excluded — only the reaper moves those back to
    `queued`.
    """
    stmt = (
        select(BuildTask)
        .where(BuildTask.status == "queued")
        .order_by(BuildTask.priority.asc(), BuildTask.created_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    task = db.execute(stmt).scalar_one_or_none()
    if task is None:
        return None

    task.status = "in_progress"
    task.claimed_at = datetime.now(UTC)
    task.claimed_by = claimed_by
    db.flush()
    log.info(
        "build_task_claimed",
        task_id=str(task.id),
        claimed_by=claimed_by,
        attempt_count=task.attempt_count,
    )
    return task


def get_task(db: Session, task_id) -> BuildTask | None:
    """Fetch a single task by id (no lock). Read-only convenience."""
    return db.execute(select(BuildTask).where(BuildTask.id == task_id)).scalar_one_or_none()


# ── Checkpoint / progress ───────────────────────────────────────────────────────


def checkpoint_task(
    db: Session,
    task_id,
    *,
    stage: str | None = None,
    progress_note: str | None = None,
    branch: str | None = None,
) -> BuildTask | None:
    """Persist a mid-task checkpoint without changing status. Caller commits.

    The builder calls this each run after a WIP commit so a fresh session can
    re-orient from `branch` + `progress_note`. Only the fields passed (non-None)
    are written — partial checkpoints are fine. Returns None if the task is gone.
    """
    task = get_task(db, task_id)
    if task is None:
        return None
    if stage is not None:
        task.stage = stage
    if progress_note is not None:
        task.progress_note = progress_note
    if branch is not None:
        task.branch = branch
    db.flush()
    return task


# ── Terminal / release transitions ──────────────────────────────────────────────


def complete_task(db: Session, task_id, *, progress_note: str | None = None) -> BuildTask | None:
    """Mark a task `done`. Idempotent — a re-complete is a no-op. Caller commits.

    Clears the claim so the row reads cleanly in the queue. A `done` task is
    invisible to `claim_next_task`, so this is the idempotency anchor.
    """
    task = get_task(db, task_id)
    if task is None:
        return None
    task.status = "done"
    task.claimed_at = None
    task.claimed_by = None
    db.flush()
    log.info("build_task_completed", task_id=str(task.id))
    return task


def release_task(db: Session, task_id, *, progress_note: str | None = None) -> BuildTask | None:
    """Return an in-progress task to `queued` WITHOUT bumping attempt_count.

    This is the soft-exit-on-limit path: a Claude usage limit is NOT a failure,
    so the task stays fully resumable and the schedule picks it up next tick.
    Bumping attempt_count here would wrongly march a perfectly-healthy task
    toward `blocked` just because the founder's daily limit reset slowly.
    Caller commits.
    """
    task = get_task(db, task_id)
    if task is None:
        return None
    if progress_note is not None:
        task.progress_note = progress_note
    task.status = "queued"
    task.claimed_at = None
    task.claimed_by = None
    db.flush()
    log.info("build_task_released", task_id=str(task.id), attempt_count=task.attempt_count)
    return task


def fail_task(db: Session, task_id, *, attempt_cap: int = ATTEMPT_CAP) -> BuildTask | None:
    """Record a genuine failure: bump attempt_count, re-queue OR block.

    A hard error (non-zero exit that is NOT a usage limit) bumps attempt_count.
    Once it reaches the cap the task is `blocked` (a human must look — no
    infinite retry loop). Below the cap it returns to `queued` to be retried.
    Caller commits.
    """
    task = get_task(db, task_id)
    if task is None:
        return None
    task.attempt_count += 1
    if task.attempt_count >= attempt_cap:
        task.status = "blocked"
        log.warning(
            "build_task_blocked",
            task_id=str(task.id),
            attempt_count=task.attempt_count,
            attempt_cap=attempt_cap,
        )
    else:
        task.status = "queued"
        log.info(
            "build_task_failed_requeued",
            task_id=str(task.id),
            attempt_count=task.attempt_count,
        )
    task.claimed_at = None
    task.claimed_by = None
    db.flush()
    return task


def block_task(db: Session, task_id) -> BuildTask | None:
    """Force a task to `blocked` (manual escalation). Caller commits."""
    task = get_task(db, task_id)
    if task is None:
        return None
    task.status = "blocked"
    task.claimed_at = None
    task.claimed_by = None
    db.flush()
    return task


def reset_task(db: Session, task_id) -> BuildTask | None:
    """Force a task back to `queued` and clear its attempt count (human re-queue).

    Used to un-block a `blocked` task after a human fixes whatever wedged it.
    Caller commits.
    """
    task = get_task(db, task_id)
    if task is None:
        return None
    task.status = "queued"
    task.attempt_count = 0
    task.claimed_at = None
    task.claimed_by = None
    db.flush()
    return task


# ── Reaper support ──────────────────────────────────────────────────────────────


def find_stale_in_progress(
    db: Session,
    *,
    threshold_min: int = STALE_THRESHOLD_MIN,
    now: datetime | None = None,
) -> list[BuildTask]:
    """Return in_progress tasks claimed longer than `threshold_min` ago.

    The reaper's candidate set. A row with claimed_at == NULL (shouldn't happen
    for in_progress, but defensive) is excluded — we only reap rows we can prove
    are stale by timestamp. Read-only; the reaper decides what to do per row.
    """
    cutoff = (now or datetime.now(UTC)) - timedelta(minutes=threshold_min)
    stmt = (
        select(BuildTask)
        .where(
            BuildTask.status == "in_progress",
            BuildTask.claimed_at.is_not(None),
            BuildTask.claimed_at < cutoff,
        )
        .order_by(BuildTask.claimed_at.asc())
    )
    return list(db.execute(stmt).scalars().all())


def reap_stale_task(db: Session, task, *, attempt_cap: int = ATTEMPT_CAP) -> str:
    """Reset ONE stale in_progress task. Returns the resulting status.

    Bumps attempt_count (the run that claimed it died without finishing, which
    counts as an attempt). If that trips the cap → `blocked` (wedged, needs a
    human); otherwise → `queued` (resumable next tick). This is what guarantees
    a task can't wedge forever AND can't loop forever. Caller commits.
    """
    task.attempt_count += 1
    if task.attempt_count >= attempt_cap:
        task.status = "blocked"
        log.warning(
            "build_task_reaped_blocked",
            task_id=str(task.id),
            attempt_count=task.attempt_count,
            attempt_cap=attempt_cap,
        )
    else:
        task.status = "queued"
        log.info(
            "build_task_reaped_requeued",
            task_id=str(task.id),
            attempt_count=task.attempt_count,
        )
    task.claimed_at = None
    task.claimed_by = None
    db.flush()
    return task.status


# ── Listing / heartbeat support ─────────────────────────────────────────────────


def list_tasks(
    db: Session,
    *,
    status: str | None = None,
    limit: int = 100,
) -> list[BuildTask]:
    """List tasks, optionally filtered by status, newest-claimed/created first."""
    stmt = select(BuildTask)
    if status is not None:
        stmt = stmt.where(BuildTask.status == status)
    stmt = stmt.order_by(BuildTask.priority.asc(), BuildTask.created_at.asc()).limit(limit)
    return list(db.execute(stmt).scalars().all())


def status_counts(db: Session) -> dict[str, int]:
    """Per-status row counts for the heartbeat digest (queued/in_progress/...)."""
    from sqlalchemy import func

    rows = db.execute(select(BuildTask.status, func.count()).group_by(BuildTask.status)).all()
    counts = {s: 0 for s in BuildTask.STATUSES}
    for status, count in rows:
        counts[status] = int(count)
    return counts
