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


def claim_next_gating_task(
    db: Session, *, claimed_by: str | None = None
) -> BuildTask | None:
    """Atomically claim the oldest UNCLAIMED `gating` task, or None.

    The gate tick's analogue of `claim_next_task`: the builder leaves a built
    task in `gating` with claimed_at=NULL (see `start_gating`); a gate tick
    grabs it via the same `FOR UPDATE SKIP LOCKED` primitive so two overlapping
    gate ticks never run the same gate. Status STAYS `gating` (claimed_at marks
    it as being worked); `open_pr`/`gate_failed` move it out. Caller commits.
    """
    stmt = (
        select(BuildTask)
        .where(BuildTask.status == "gating", BuildTask.claimed_at.is_(None))
        .order_by(BuildTask.priority.asc(), BuildTask.created_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    task = db.execute(stmt).scalar_one_or_none()
    if task is None:
        return None
    task.claimed_at = datetime.now(UTC)
    task.claimed_by = claimed_by
    db.flush()
    log.info("build_task_gating_claimed", task_id=str(task.id), claimed_by=claimed_by)
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


def start_gating(
    db: Session, task_id, *, head_sha: str, branch: str | None = None
) -> BuildTask | None:
    """Flip a built `in_progress` task to `gating` so a gate tick can pick it up.

    Called by the builder where it used to call `complete` (on TASK COMPLETE).
    Records `head_sha` (the exact pushed commit) so the gate tick can assert it
    is gating the tree the builder actually pushed — never a stale/partial push.
    CLEARS the claim so `claim_next_gating_task` can atomically grab it; the
    builder is done with this row. Caller commits.
    """
    task = get_task(db, task_id)
    if task is None:
        return None
    task.status = "gating"
    task.head_sha = head_sha
    if branch is not None:
        task.branch = branch
    task.claimed_at = None
    task.claimed_by = None
    db.flush()
    log.info("build_task_start_gating", task_id=str(task.id), head_sha=head_sha)
    return task


def open_pr(
    db: Session,
    task_id,
    *,
    pr_url: str,
    pr_number: int | None = None,
    gate_report: dict | None = None,
    branch: str | None = None,
) -> BuildTask | None:
    """Gates green: flip `gating` → `awaiting_approval` and record the PR.

    The resting state for Phase 2 (the founder merges the PR by hand) AND the
    queue Phase 3's phone surface reads — same rows, no rename. Idle from here:
    the reaper never touches `awaiting_approval`; the digest surfaces it. Clears
    the claim. Caller commits.
    """
    task = get_task(db, task_id)
    if task is None:
        return None
    task.status = "awaiting_approval"
    task.pr_url = pr_url
    if pr_number is not None:
        task.pr_number = pr_number
    if gate_report is not None:
        task.gate_report = gate_report
    if branch is not None:
        task.branch = branch
    task.claimed_at = None
    task.claimed_by = None
    db.flush()
    log.info("build_task_pr_opened", task_id=str(task.id), pr_url=pr_url)
    return task


def gate_failed(
    db: Session,
    task_id,
    *,
    gate_report: dict | None = None,
    progress_note: str | None = None,
    attempt_cap: int = ATTEMPT_CAP,
) -> BuildTask | None:
    """A BLOCKING gate failed (tests red, overlays clipped): record + fail.

    Distinct from a gate-tick ABORT (timeout / Docker OOM), which is infra, not
    the code's fault — the gate runner calls `release_task` for that (no bump).
    A real gate failure means the built chunk isn't good enough; it bumps
    attempt_count and re-queues for another builder chunk to fix, blocking at the
    cap so a persistently-failing task escalates to a human instead of looping.
    Records `gate_report`/`progress_note` first (so the next chunk knows WHAT
    failed), then delegates the bump/route to `fail_task` (DRY). Caller commits.
    """
    task = get_task(db, task_id)
    if task is None:
        return None
    if gate_report is not None:
        task.gate_report = gate_report
    if progress_note is not None:
        task.progress_note = progress_note
    db.flush()
    return fail_task(db, task_id, attempt_cap=attempt_cap)


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


def reap_stale_task(
    db: Session,
    task,
    *,
    attempt_cap: int = ATTEMPT_CAP,
    requeue_status: str = "queued",
) -> str:
    """Reset ONE stale claimed task. Returns the resulting status.

    Bumps attempt_count (the run that claimed it died without finishing, which
    counts as an attempt). If that trips the cap → `blocked` (wedged, needs a
    human); otherwise → `requeue_status`. This is what guarantees a task can't
    wedge forever AND can't loop forever. Caller commits.

    `requeue_status` is "queued" for a stale `in_progress` builder run (resume
    from the builder), but "gating" for a stale `gating` gate run — the code is
    already built + pushed, so re-running the GATE (not the builder) is the
    right resume. Either way the cap routes a persistently-dying task to blocked.
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
        task.status = requeue_status
        log.info(
            "build_task_reaped_requeued",
            task_id=str(task.id),
            attempt_count=task.attempt_count,
            requeue_status=requeue_status,
        )
    task.claimed_at = None
    task.claimed_by = None
    db.flush()
    return task.status


def find_stale_gating(
    db: Session,
    *,
    threshold_min: int = STALE_THRESHOLD_MIN,
    now: datetime | None = None,
) -> list[BuildTask]:
    """Return CLAIMED `gating` tasks whose gate run died (claimed_at too old).

    A gate tick that claimed a `gating` row and then died (Docker OOM mid
    verify-overlays, host slept) leaves it claimed forever — `claim_next_gating_
    task` only hands out UNCLAIMED gating rows, so it would wedge. The reaper
    re-runs these via `reap_stale_task(..., requeue_status="gating")` so another
    gate tick re-gates the already-built branch. Read-only. (Unclaimed-stale
    gating is NOT wedged — the next gate tick claims it — so the digest, not the
    reaper, surfaces it via `count_stale_unclaimed_gating`.)
    """
    cutoff = (now or datetime.now(UTC)) - timedelta(minutes=threshold_min)
    stmt = (
        select(BuildTask)
        .where(
            BuildTask.status == "gating",
            BuildTask.claimed_at.is_not(None),
            BuildTask.claimed_at < cutoff,
        )
        .order_by(BuildTask.claimed_at.asc())
    )
    return list(db.execute(stmt).scalars().all())


def count_stale_unclaimed_gating(
    db: Session,
    *,
    threshold_min: int = STALE_THRESHOLD_MIN,
    now: datetime | None = None,
) -> int:
    """Count UNCLAIMED `gating` rows sitting longer than the threshold.

    Not a wedge (the next gate tick will claim it) — a non-zero count means the
    gate tick itself isn't running (scheduler down / crashed). The digest
    surfaces this as a dead-gate-tick warning. Read-only.
    """
    from sqlalchemy import func

    cutoff = (now or datetime.now(UTC)) - timedelta(minutes=threshold_min)
    stmt = (
        select(func.count())
        .select_from(BuildTask)
        .where(
            BuildTask.status == "gating",
            BuildTask.claimed_at.is_(None),
            BuildTask.updated_at < cutoff,
        )
    )
    return int(db.execute(stmt).scalar_one())


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
