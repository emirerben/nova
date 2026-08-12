"""Job pipeline phase tracking — best-effort writes that drive the live progress UI.

Single source of truth for the user-facing phase names. Internal pipeline
log events (e.g. `_assemble_clips`'s `_phase_done`) emit fine-grained
sub-phases for telemetry; this module records the small, stable set of
top-level phases the frontend renders.

Writes are best-effort by design: every helper swallows exceptions and logs.
A failed phase write must never fail the user's job.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Final

import structlog
from sqlalchemy import cast, literal, update
from sqlalchemy.dialects.postgresql import JSONB

from app.config import settings
from app.database import sync_session
from app.models import Job

log = structlog.get_logger()

_CANCELLED_STATUS: Final[str] = "cancelled"


def _cancelled(job: Job) -> bool:
    """Cancelled is an immutable terminal state for every phase writer."""
    return job.status == _CANCELLED_STATUS


# Canonical phase names. Keep stable — the frontend maps these to user-facing
# copy in src/apps/web/src/lib/template-job-phases.ts. Adding a new phase here
# without updating that map is fine (UI falls back to a humanised title).
PHASE_QUEUED: Final[str] = "queued"
PHASE_DOWNLOAD_CLIPS: Final[str] = "download_clips"
PHASE_ANALYZE_CLIPS: Final[str] = "analyze_clips"
PHASE_MATCH_CLIPS: Final[str] = "match_clips"
PHASE_ASSEMBLE: Final[str] = "assemble"
PHASE_MIX_AUDIO: Final[str] = "mix_audio"
PHASE_GENERATE_COPY: Final[str] = "generate_copy"
PHASE_UPLOAD: Final[str] = "upload"
PHASE_FINALIZE: Final[str] = "finalize"

# Ordered for the frontend so a phase that arrives "late" still shows
# everything before it as complete (useful when the worker fires events
# faster than the frontend can render them).
PHASE_ORDER: Final[tuple[str, ...]] = (
    PHASE_QUEUED,
    PHASE_DOWNLOAD_CLIPS,
    PHASE_ANALYZE_CLIPS,
    PHASE_MATCH_CLIPS,
    PHASE_ASSEMBLE,
    PHASE_MIX_AUDIO,
    PHASE_GENERATE_COPY,
    PHASE_UPLOAD,
    PHASE_FINALIZE,
)


def _coerce_uuid(job_id: str | uuid.UUID) -> uuid.UUID | None:
    if isinstance(job_id, uuid.UUID):
        return job_id
    try:
        return uuid.UUID(str(job_id))
    except (ValueError, TypeError, AttributeError):
        return None


def mark_started(job_id: str | uuid.UUID) -> None:
    """Record that the worker picked up the job. Sets started_at + initial phase.

    Idempotent: re-running won't move started_at backwards, but will refresh
    current_phase to the first-known phase. This models WORKER PICKUP of one
    orchestrator run — a Celery redelivery of the same run must not restart the
    user's clock mid-render.

    NOT the whole story for `started_at`. A re-render reuses the SAME Job row, so
    if this were the only writer the progress UI would keep counting from the
    first render forever (a 5-minute edit displayed "40m 32s"). Re-render
    DISPATCH deliberately moves `started_at` forward — see `mark_reattempt`
    below. Anything here that derives from `started_at` (the `t_offset_ms` in
    `record_phase` / `record_sub_phase`) is therefore relative to the CURRENT
    attempt.
    """
    job_uuid = _coerce_uuid(job_id)
    if job_uuid is None:
        return
    try:
        with sync_session() as db:
            job = db.get(Job, job_uuid, with_for_update=True)
            if job is None or _cancelled(job):
                return
            now = datetime.now(UTC)
            if job.started_at is None:
                job.started_at = now
            job.current_phase = PHASE_DOWNLOAD_CLIPS
            db.commit()
    except Exception as exc:  # pragma: no cover — best-effort
        log.warning("phase_mark_started_failed", job_id=str(job_id), error=str(exc))


def mark_reattempt(job: Job) -> bool:
    """Anchor the user-facing wall clock to NOW — a re-render dispatch is a new attempt.

    Operates on an ORM object the caller already holds (route dispatchers work on
    a loaded, often row-locked Job and commit themselves), unlike the job_id-keyed
    helpers around it. Returns True when the clock moved.

    `job.started_at` is the origin `ProgressTheater` counts elapsed time from, and
    it also drives the ETA and the stall copy. A re-render reuses the SAME Job row
    and `mark_started` refuses to move `started_at` once set, so before this a
    re-render of a 5-minute edit displayed "40m 32s".

    Anchored at DISPATCH, not worker pickup: the user's mental model is the Save
    press, and queue wait is time they are genuinely waiting.

    SKIPS the reset while an orchestrator run is in flight (`current_phase` is
    non-None until `mark_finished` clears it). Editing an already-ready variant
    while its siblings are still on their FIRST render would otherwise move the
    anchor out from under that run: every subsequent `record_phase` would compute
    `t_offset_ms` against the new origin (a non-monotonic `phase_log`), and the
    whole-job clock the user is watching for the siblings would visibly jump back
    to zero mid-render.

    Deliberately does NOT touch `finished_at`: `plan_items.py` exports it as the
    plan item's ready date and no re-render task calls `mark_finished`, so nulling
    it here would erase that date permanently. Readers guard on
    `started_at > finished_at` instead.

    Concurrent Saves on sibling variants after the first render completes: the LAST
    dispatch owns the clock. That is the requested behavior ("every Save restarts
    it"); the per-variant tiles keep their own `render_started_at`.
    """
    if getattr(job, "status", None) == _CANCELLED_STATUS:
        return False
    # getattr, not attribute access: this is a best-effort UI concern and callers
    # include lightweight Job stand-ins. A missing attribute means "no run in
    # flight", which is the safe reading — the clock moves.
    if getattr(job, "current_phase", None) is not None:
        return False
    job.started_at = datetime.now(UTC)
    return True


def stamp_variant_attempt(variant: dict) -> None:
    """Mark ONE variant dict as a freshly-started render attempt, in place.

    Takes the DICT rather than (job, variant_id) on purpose: several dispatchers
    build a copy (`updated = dict(v)`) and then write `variants[i] = updated`. A
    helper that re-walked the job's variant list would mutate the original and have
    its write silently overwritten by that assignment — no error, no failing test.
    Passing the dict the caller actually persists makes that class of bug
    unrepresentable.

    `render_started_at` is what the per-variant clocks read (`VariantRenderCard`'s
    "Rendering · m:ss" and the hero rendering label's stall hint). It used to be
    written in exactly ONE place repo-wide — the initial render loop in
    `tasks/generative_build.py` — so every re-render inherited the first render's
    timestamp and tripped the 5-minute "Taking longer than usual…" hint instantly.

    Does NOT write `render_enqueued_at`; that field's only writer is
    `_mark_variant_rendering`, which owns the caption-reburn supersession token.
    Note the enqueue/start pair in `render_summary._variant_queue_ms` cannot
    measure real queue latency for re-renders either way: no re-render task stamps
    `render_started_at` at worker pickup, so that metric reads ~0 on every
    re-render path. See TODOS.md.
    """
    variant["render_status"] = "rendering"
    # Naive-UTC + literal "Z" is the frozen wire format for this JSONB field
    # (25 sibling call sites, incl. the first-render loop). `datetime.now(UTC)`
    # would serialize "+00:00" and appending "Z" to that is malformed.
    variant["render_started_at"] = datetime.now(UTC).replace(tzinfo=None).isoformat() + "Z"


def record_phase(
    job_id: str | uuid.UUID,
    name: str,
    *,
    elapsed_ms: int | None = None,
    next_phase: str | None = None,
) -> None:
    """Append a completed phase to phase_log and set the next live phase.

    Args:
        job_id: target job
        name: the phase that just completed
        elapsed_ms: wall time of the completed phase (optional)
        next_phase: the new live phase to surface (optional — if omitted the
            frontend continues showing `name` until the next call)
    """
    job_uuid = _coerce_uuid(job_id)
    if job_uuid is None:
        return
    try:
        with sync_session() as db:
            job = db.get(Job, job_uuid, with_for_update=True)
            if job is None or _cancelled(job):
                return
            now = datetime.now(UTC)
            t_offset_ms = None
            if job.started_at is not None:
                t_offset_ms = int((now - job.started_at).total_seconds() * 1000)
            entry = {
                "name": name,
                "elapsed_ms": int(elapsed_ms) if elapsed_ms is not None else None,
                "t_offset_ms": t_offset_ms,
                "ts": now.isoformat(),
            }
            # JSONB list append. Reassigning a NEW list is required so
            # SQLAlchemy detects the change — in-place .append() on the
            # mutable JSONB column does NOT mark it dirty.
            existing = list(job.phase_log or [])
            existing.append(entry)
            job.phase_log = existing
            if next_phase is not None:
                job.current_phase = next_phase
            db.commit()
    except Exception as exc:  # pragma: no cover — best-effort
        log.warning(
            "phase_record_failed",
            job_id=str(job_id),
            phase=name,
            error=str(exc),
        )


def record_sub_phase(
    job_id: str | uuid.UUID,
    parent: str,
    name: str,
    *,
    elapsed_ms: int | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Atomically append a sub-phase entry to phase_log.

    Sub-phases are used to surface per-clip / per-step timing inside a parent
    phase like ``analyze_clips``. Worker threads call this concurrently, so we
    use Postgres' JSONB ``||`` operator inside a single UPDATE to avoid the
    read-modify-write race that ``record_phase`` has (acceptable there because
    top-level phases are sequential, not so here).
    """
    job_uuid = _coerce_uuid(job_id)
    if job_uuid is None:
        return
    now = datetime.now(UTC)
    entry: dict[str, Any] = {
        "name": name,
        "parent": parent,
        "elapsed_ms": int(elapsed_ms) if elapsed_ms is not None else None,
        "ts": now.isoformat(),
    }
    if detail is not None:
        entry["detail"] = detail
    try:
        with sync_session() as db:
            job = db.get(Job, job_uuid, with_for_update=True)
            if job is None or _cancelled(job):
                return
            if job.started_at is not None:
                entry["t_offset_ms"] = int((now - job.started_at).total_seconds() * 1000)
            stmt = (
                update(Job)
                .where(Job.id == job_uuid, Job.status != _CANCELLED_STATUS)
                .values(phase_log=Job.phase_log.op("||")(cast(literal(json.dumps([entry])), JSONB)))
            )
            db.execute(stmt)
            db.commit()
    except Exception as exc:  # pragma: no cover — best-effort
        log.warning(
            "sub_phase_record_failed",
            job_id=str(job_id),
            parent=parent,
            name=name,
            error=str(exc),
        )


def mark_finished(job_id: str | uuid.UUID) -> None:
    """Stamp finished_at and clear current_phase. Called on terminal success."""
    job_uuid = _coerce_uuid(job_id)
    if job_uuid is None:
        return
    try:
        with sync_session() as db:
            job = db.get(Job, job_uuid, with_for_update=True)
            if job is None or _cancelled(job):
                return
            job.finished_at = datetime.now(UTC)
            job.current_phase = None
            db.commit()
    except Exception as exc:  # pragma: no cover — best-effort
        log.warning("phase_mark_finished_failed", job_id=str(job_id), error=str(exc))


class PhaseTimer:
    """Context manager — start = enter, complete = exit-without-exception.

    Usage:
        with PhaseTimer(job_id, PHASE_DOWNLOAD_CLIPS, next_phase=PHASE_ANALYZE_CLIPS):
            ...

    If the block raises, the phase is NOT recorded (the failure handler
    above will clear current_phase via mark_finished_failed paths).
    """

    def __init__(
        self,
        job_id: str | uuid.UUID,
        phase: str,
        *,
        next_phase: str | None = None,
    ) -> None:
        self.job_id = job_id
        self.phase = phase
        self.next_phase = next_phase
        self._t0 = 0.0

    def __enter__(self) -> PhaseTimer:
        self._t0 = time.monotonic()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is not None:
            return
        elapsed_ms = int((time.monotonic() - self._t0) * 1000)
        record_phase(
            self.job_id,
            self.phase,
            elapsed_ms=elapsed_ms,
            next_phase=self.next_phase,
        )


def beat_heartbeat(job_id: str | uuid.UUID) -> None:
    """Tick jobs.worker_heartbeat_at to the DB clock (func.now()). Best-effort.

    Postgres is the single clock source for the beacon on purpose: the worker
    VM writes it and the API VM reads it, so a worker-clock timestamp would
    let cross-VM skew shift the staleness window (false `retrying: true` on a
    slow clock, masked stalls on a fast one). The reader still compares with
    its own app clock — that halves the skew sources; API↔DB skew is
    NTP-bounded and ≪ the 150s threshold.

    Deliberately a bare read-free UPDATE — the heartbeat thread runs
    concurrently with the orchestrator's row-locked assembly_plan
    read-modify-writes, so it must never read-modify-write any JSONB state.
    Note it is not literally single-column: the model's `updated_at`
    onupdate=func.now() fires on every UPDATE, so beats also refresh
    updated_at. That is deliberate — a beating worker keeps the row visibly
    fresh (the reaper's `updated_at < cutoff` staleness gate only ever fires
    on rows whose beats have stopped).
    """
    job_uuid = _coerce_uuid(job_id)
    if job_uuid is None:
        return
    try:
        from sqlalchemy import func  # noqa: PLC0415

        with sync_session() as db:
            db.execute(
                update(Job)
                .where(Job.id == job_uuid, Job.status != _CANCELLED_STATUS)
                .values(worker_heartbeat_at=func.now())
            )
            db.commit()
    except Exception as exc:  # pragma: no cover — best-effort
        log.warning("job_heartbeat_failed", job_id=str(job_id), error=str(exc))


@contextmanager
def job_heartbeat(job_id: str | uuid.UUID) -> Iterator[None]:
    """Tick the job's liveness beacon on a daemon thread while the body runs.

    Why (2026-07-21 OOM incident, job e8173a25): an OOM-killed worker leaves
    its job at status="rendering" with zero signal until the acks_late
    redelivery fires (visibility_timeout=1900s) — 30+ minutes of a dead
    attempt looking identical to healthy progress. The status route compares
    this beacon against now(); once it goes stale it reports
    `retrying: true`, and the redelivered attempt's FIRST beat (synchronous,
    below) clears the flag immediately on resume.

    The thread is a daemon and the interval wait doubles as the stop signal,
    so a SIGKILL'd worker simply stops beating — which is exactly the signal.
    """
    beat_heartbeat(job_id)  # immediate: a redelivered attempt un-stales at once
    stop = threading.Event()
    interval = max(5, int(settings.render_heartbeat_interval_s))

    def _loop() -> None:
        while not stop.wait(interval):
            beat_heartbeat(job_id)

    thread = threading.Thread(target=_loop, daemon=True, name=f"job-heartbeat-{str(job_id)[:8]}")
    thread.start()
    try:
        yield
    finally:
        stop.set()
        # A beat blocked on a row lock can outlive the 2s join: the daemon
        # thread then writes ONE final beat after the caller's terminal status
        # write and exits (stop is set). Harmless by construction — the status
        # route checks job.status before beacon age, so a fresh beacon on a
        # terminal row can never resurrect `retrying` — but that ordering is
        # the load-bearing contract if _compute_retrying is ever refactored.
        thread.join(timeout=2)


def mark_failed_phase(job_id: str | uuid.UUID) -> None:
    """Clear current_phase + stamp finished_at on terminal failure."""
    job_uuid = _coerce_uuid(job_id)
    if job_uuid is None:
        return
    try:
        with sync_session() as db:
            stmt = (
                update(Job)
                .where(Job.id == job_uuid, Job.status != _CANCELLED_STATUS)
                .values(
                    current_phase=None,
                    finished_at=datetime.now(UTC),
                )
            )
            db.execute(stmt)
            db.commit()
    except Exception as exc:  # pragma: no cover — best-effort
        log.warning("phase_mark_failed_failed", job_id=str(job_id), error=str(exc))
