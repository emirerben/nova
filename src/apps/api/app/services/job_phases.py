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
import time
import uuid
from datetime import UTC, datetime
from typing import Any, Final

import structlog
from sqlalchemy import cast, literal, update
from sqlalchemy.dialects.postgresql import JSONB

from app.database import sync_session
from app.models import Job

log = structlog.get_logger()


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
    current_phase to the first-known phase.
    """
    job_uuid = _coerce_uuid(job_id)
    if job_uuid is None:
        return
    try:
        with sync_session() as db:
            job = db.get(Job, job_uuid)
            if job is None:
                return
            now = datetime.now(UTC)
            if job.started_at is None:
                job.started_at = now
            job.current_phase = PHASE_DOWNLOAD_CLIPS
            db.commit()
    except Exception as exc:  # pragma: no cover — best-effort
        log.warning("phase_mark_started_failed", job_id=str(job_id), error=str(exc))


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
            job = db.get(Job, job_uuid)
            if job is None:
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
            job = db.get(Job, job_uuid)
            if job is None:
                return
            if job.started_at is not None:
                entry["t_offset_ms"] = int(
                    (now - job.started_at).total_seconds() * 1000
                )
            stmt = (
                update(Job)
                .where(Job.id == job_uuid)
                .values(
                    phase_log=Job.phase_log.op("||")(
                        cast(literal(json.dumps([entry])), JSONB)
                    )
                )
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
            job = db.get(Job, job_uuid)
            if job is None:
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


def mark_failed_phase(job_id: str | uuid.UUID) -> None:
    """Clear current_phase + stamp finished_at on terminal failure."""
    job_uuid = _coerce_uuid(job_id)
    if job_uuid is None:
        return
    try:
        with sync_session() as db:
            stmt = (
                update(Job)
                .where(Job.id == job_uuid)
                .values(
                    current_phase=None,
                    finished_at=datetime.now(UTC),
                )
            )
            db.execute(stmt)
            db.commit()
    except Exception as exc:  # pragma: no cover — best-effort
        log.warning("phase_mark_failed_failed", job_id=str(job_id), error=str(exc))
