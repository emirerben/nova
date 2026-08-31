"""On-demand repair of one library job's missing preview poster.

Why this exists — the library grid (`GET /me/jobs`) classifies a ready job with
an owned video path but no stored poster as ``poster_status="repairing"``.
Before this task nothing in the system repaired it: poster generation only ever
ran at render time (from 2026-08-27), and the one bulk repairer
(`scripts/backfill_video_posters.py`) is a manual GitHub-workflow dispatch. So
every pre-2026-08-27 tile reported "repairing" forever and the client spun
forever. `POST /me/jobs/posters/refresh` now enqueues this task, which makes
"repairing" a true statement.

Discipline mirrored from `scripts/backfill_video_posters.py` (deliberately NOT
imported — a Celery task must not depend on a one-shot ops script, and the
script's flow is snapshot/batch shaped):

* Every read and write of the Job row happens under ``SELECT ... FOR UPDATE``.
* Extraction/upload happens with NO lock held (a full MP4 download can take
  a minute; holding a row lock across it would block deletes and re-renders).
* After upload the row is re-locked and re-verified — same source key, poster
  field still empty, render identity unchanged. A failed verification discards
  the freshly uploaded object instead of clobbering a concurrent render.
* JSONB is not recursively mutation-tracked: deep-copy ``assembly_plan`` before
  any nested write and call ``flag_modified``. Skipping either produces a
  successful commit that persists NOTHING (backfill_video_posters.py:777-783).
* The persisted value is always the RELATIVE object key — never a signed URL.

Layering: this module must not import from ``app.routes.*`` (see the module
docstring of ``app/services/job_storage_paths.py``). The preview-resolution
order below therefore duplicates ``routes/me.py::_preview`` on purpose; keeping
the two in agreement is a stated consolidation TODO.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from typing import Any

import structlog

from app.worker import celery_app

log = structlog.get_logger()

# Namespace on ``Job.assembly_plan`` for the repair marker. Task-owned JSONB:
# a concurrent reburn's read-modify-write can drop it, which costs exactly one
# extra repair cycle on the next library view (self-correcting) — which is why
# this is a marker and not a side table.
POSTER_REPAIR_MARKER_FIELD = "_poster_repair"

# After this many real extraction failures the marker goes terminal and the
# tile reads an honest "Thumbnail unavailable" instead of spinning forever.
MAX_POSTER_REPAIR_ATTEMPTS = 3

TERMINAL_EXPIRED_SOURCE = "expired_source"
TERMINAL_ATTEMPTS_EXHAUSTED = "attempts_exhausted"

# Mirrors `_MAX_PREVIEW_CLIPS` in routes/me.py — bound the clip scan for the
# legacy/default JobClip-backed job shape.
_MAX_PREVIEW_CLIPS = 50

_SOURCE_KIND = "poster_repair"


@dataclass
class _RepairTarget:
    """The single preview object this job's library tile renders."""

    kind: str  # variant | job_output | job_clip
    source_key: str
    poster_field: str
    poster_value: Any = None
    variant_id: str | None = None
    variant_index: int | None = None
    render_identity: Any = None
    clip_id: uuid.UUID | None = None
    # Live references into the freshly locked state, used only for writes.
    variant: dict[str, Any] | None = field(default=None, repr=False)
    clip: Any = field(default=None, repr=False)

    @property
    def identity(self) -> tuple:
        """Comparable snapshot of everything a re-render would change."""
        return (
            self.kind,
            self.source_key,
            self.variant_id,
            # Index only disambiguates variants that carry no id at all.
            None if self.variant_id else self.variant_index,
            repr(self.render_identity),
            str(self.clip_id) if self.clip_id else None,
        )


def _variant_rank(variant: dict, fallback: int) -> tuple[int, int]:
    """Stable rank key for task-owned variant dicts (copy of the route's rule)."""
    raw = variant.get("rank")
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw, fallback
    return 1_000_000 + fallback, fallback


def _resolve_target(job: Any, plan: dict[str, Any], clips: list[Any]) -> _RepairTarget | None:
    """Ready variants by rank → top-level plan output → lowest-ranked ready clip.

    ``plan`` is passed separately so callers can resolve against the deep copy
    they are about to mutate, keeping the returned variant reference writable.
    """
    from app.services.job_storage_paths import owned_job_output_path  # noqa: PLC0415

    variants = plan.get("variants")
    if isinstance(variants, list):
        ready = [
            (index, variant)
            for index, variant in enumerate(variants)
            if isinstance(variant, dict) and variant.get("render_status") == "ready"
        ]
        for index, variant in sorted(ready, key=lambda item: _variant_rank(item[1], item[0])):
            source_key = owned_job_output_path(
                variant.get("video_path") or variant.get("output_url"), job
            )
            if not source_key:
                # Legacy rows that only ever stored a foreign/expired signed URL
                # have no owned source object to extract from. `_preview` shows
                # them as terminally unavailable, so they are never enqueued.
                continue
            return _RepairTarget(
                kind="variant",
                source_key=source_key,
                poster_field="poster_path",
                poster_value=variant.get("poster_path"),
                variant_id=str(variant.get("variant_id") or "") or None,
                variant_index=index,
                render_identity=(
                    variant.get("render_generation_id") or variant.get("render_finished_at")
                ),
                variant=variant,
            )

    source_key = owned_job_output_path(
        plan.get("output_path") or plan.get("video_path") or plan.get("output_url"), job
    )
    if source_key:
        return _RepairTarget(
            kind="job_output",
            source_key=source_key,
            poster_field="poster_path",
            poster_value=plan.get("poster_path"),
        )

    for clip in sorted(clips or [], key=lambda item: (item.rank, str(item.id))):
        if clip.render_status != "ready":
            continue
        source_key = owned_job_output_path(clip.video_path, job)
        if source_key:
            return _RepairTarget(
                kind="job_clip",
                source_key=source_key,
                poster_field="thumbnail_path",
                poster_value=clip.thumbnail_path,
                clip_id=clip.id,
                clip=clip,
            )
    return None


def _plan_of(job: Any) -> dict[str, Any] | None:
    """Deep copy of the JSONB plan, or None when the column is corrupt.

    A non-dict, non-null ``assembly_plan`` is forensic state: coercing it to
    ``{}`` would silently destroy it, so every caller bails instead.
    """
    if job.assembly_plan is None:
        return {}
    if not isinstance(job.assembly_plan, dict):
        return None
    return copy.deepcopy(job.assembly_plan)


def _marker_of(plan: dict[str, Any]) -> dict[str, Any]:
    marker = plan.get(POSTER_REPAIR_MARKER_FIELD)
    return dict(marker) if isinstance(marker, dict) else {}


def _marker_attempts(marker: dict[str, Any]) -> int:
    raw = marker.get("attempts")
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
        return raw
    return 0


def _stage_plan(job: Any, plan: dict[str, Any]) -> None:
    """Assign the mutated copy AND flag the JSONB column dirty.

    Missing `flag_modified` here is the silent no-op-commit bug documented at
    backfill_video_posters.py:777-783 — SQLAlchemy sees no change, commits no
    UPDATE, and the caller reports a success that never reached the database.
    """
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

    job.assembly_plan = plan
    flag_modified(job, "assembly_plan")


def _write_marker(
    plan: dict[str, Any],
    *,
    video_path: str,
    attempts: int,
    terminal: str | None,
    enqueued_at: str | None,
) -> None:
    plan[POSTER_REPAIR_MARKER_FIELD] = {
        "video_path": video_path,
        "attempts": attempts,
        "terminal": terminal,
        "enqueued_at": enqueued_at,
    }


def _ready_clips(db: Any, job_id: uuid.UUID) -> list[Any]:
    from sqlalchemy import select  # noqa: PLC0415

    from app.models import JobClip  # noqa: PLC0415

    return list(
        db.execute(
            select(JobClip)
            .where(JobClip.job_id == job_id, JobClip.render_status == "ready")
            .order_by(JobClip.rank, JobClip.created_at, JobClip.id)
            .limit(_MAX_PREVIEW_CLIPS)
        )
        .scalars()
        .all()
    )


def _owned_poster(job: Any, value: Any) -> str | None:
    from app.services.job_storage_paths import owned_job_output_path  # noqa: PLC0415

    return owned_job_output_path(value, job)


@celery_app.task(
    name="tasks.repair_job_poster",
    bind=True,
    max_retries=0,
    # Extraction is a bounded download + one FFmpeg seek; 150s soft / 180s hard
    # sits far under the broker's 1900s visibility_timeout invariant
    # (tests/tasks/test_task_time_limits.py).
    soft_time_limit=150,
    time_limit=180,
    acks_late=True,
    reject_on_worker_lost=True,
)
def repair_job_poster(self, job_id: str) -> str:  # noqa: ANN001, ARG001
    """Generate + persist the missing poster for one job. Never raises.

    Returns a short outcome string for logs and tests. Fire-and-forget: the
    library re-reads the persisted key on the next refresh.
    """
    try:
        outcome = _run_repair(job_id)
    except Exception as exc:  # noqa: BLE001 — poster repair is fail-open by contract
        log.warning(
            "poster_repair_failed",
            job_id=job_id,
            error_class=type(exc).__name__,
            error=str(exc)[:300],
        )
        return "error"
    log.info("poster_repair_outcome", job_id=job_id, outcome=outcome)
    return outcome


def _run_repair(job_id: str) -> str:
    """The repair body. Separated so tests drive it without the Celery wrapper."""
    from app.config import settings  # noqa: PLC0415

    if not settings.poster_ondemand_repair_enabled:
        # A task queued before the kill switch was flipped drains harmlessly.
        return "disabled"

    from app.database import sync_session  # noqa: PLC0415
    from app.models import Job, JobClip  # noqa: PLC0415
    from app.services import template_poster  # noqa: PLC0415
    from app.services.job_status import PLAN_ITEM_JOB_READY  # noqa: PLC0415
    from app.storage import delete_object_best_effort, object_exists  # noqa: PLC0415

    try:
        jid = uuid.UUID(str(job_id))
    except (TypeError, ValueError):
        return "bad_id"

    # ── Phase 1: locked resolution + source pre-check ────────────────────────
    with sync_session() as db:
        job = db.get(Job, jid, with_for_update=True)
        if job is None or job.status == "cancelled" or job.status not in PLAN_ITEM_JOB_READY:
            return "not_repairable"

        plan = _plan_of(job)
        if plan is None:
            return "corrupt_plan"

        # Clips are only the third resolution tier, so their (indexed) query is
        # skipped entirely for variant/plan-backed jobs.
        target = _resolve_target(job, plan, [])
        if target is None:
            target = _resolve_target(job, plan, _ready_clips(db, jid))
        if target is None:
            return "no_preview"

        if _owned_poster(job, target.poster_value):
            # A concurrent render (or an earlier repair) already won.
            return "already_present"

        marker = _marker_of(plan)
        # A marker bound to a DIFFERENT source belongs to a superseded render:
        # its attempt count and its verdict are both void, and every write
        # below rebinds it to the current source with a clean slate. No commit
        # is needed for the rebind itself — the next marker write does it.
        rebound = marker.get("video_path") != target.source_key
        attempts = 0 if rebound else _marker_attempts(marker)
        if not rebound and marker.get("terminal"):
            return "terminal"

        snapshot = target.identity
        source_key = target.source_key
        marker_enqueued_at = marker.get("enqueued_at")

    # ── Phase 1b: source probe with NO lock held ─────────────────────────────
    # Network I/O never happens under the row lock (module contract above):
    # a degraded GCS would otherwise stretch a millisecond lock into a
    # multi-second one that blocks this job's deletes and re-renders.
    if not object_exists(source_key):
        # Lifecycle-expired source (music-jobs/* at 24h, jobs/* at 30d).
        # Nothing can ever be extracted: settle terminal so the tile reads
        # "Thumbnail unavailable" instead of spinning.
        with sync_session() as db:
            job = db.get(Job, jid, with_for_update=True)
            if job is None:
                return "stale_race"
            plan = _plan_of(job)
            if plan is None:
                return "corrupt_plan"
            current = _resolve_target(job, plan, [])
            if current is None:
                current = _resolve_target(job, plan, _ready_clips(db, jid))
            if current is None or current.identity != snapshot:
                # Re-rendered under us: the new source has not been probed, so
                # marking it terminal here would be a verdict on stale evidence.
                return "stale_race"
            _write_marker(
                plan,
                video_path=source_key,
                attempts=attempts,
                terminal=TERMINAL_EXPIRED_SOURCE,
                enqueued_at=marker_enqueued_at,
            )
            _stage_plan(job, plan)
            db.commit()
        return TERMINAL_EXPIRED_SOURCE

    # ── Phase 2: extract + upload with NO lock held ──────────────────────────
    poster_key = template_poster.generate_and_upload_from_gcs(
        source_key,
        job_id=str(jid),
        source_kind=_SOURCE_KIND,
    )

    if not poster_key:
        with sync_session() as db:
            job = db.get(Job, jid, with_for_update=True)
            if job is None:
                return "stale_race"
            plan = _plan_of(job)
            if plan is None:
                return "corrupt_plan"
            current = _resolve_target(job, plan, [])
            if current is None:
                current = _resolve_target(job, plan, _ready_clips(db, jid))
            if current is None or current.source_key != source_key:
                # Re-rendered under us: charging the new source for the old
                # source's failure could mint a false terminal.
                return "stale_race"
            marker = _marker_of(plan)
            bound = marker.get("video_path") == source_key
            # `attempts` increments ONLY here, on a real extraction failure —
            # never at enqueue time, so a deploy or worker death can never mint
            # a false `attempts_exhausted`.
            attempts = (_marker_attempts(marker) if bound else 0) + 1
            terminal = (
                TERMINAL_ATTEMPTS_EXHAUSTED if attempts >= MAX_POSTER_REPAIR_ATTEMPTS else None
            )
            _write_marker(
                plan,
                video_path=source_key,
                attempts=attempts,
                terminal=terminal,
                enqueued_at=marker.get("enqueued_at") if bound else None,
            )
            _stage_plan(job, plan)
            db.commit()
            return terminal or "extract_failed"

    # ── Phase 3: re-lock, re-verify, persist the relative key ────────────────
    with sync_session() as db:
        job = db.get(Job, jid, with_for_update=True)
        stale = job is None or job.status == "cancelled" or job.status not in PLAN_ITEM_JOB_READY
        plan = None if stale else _plan_of(job)
        target = None
        if plan is not None:
            target = _resolve_target(job, plan, [])
            if target is None:
                target = _resolve_target(job, plan, _ready_clips(db, jid))

        if (
            target is None
            or target.identity != snapshot
            or _owned_poster(job, target.poster_value)
            or not _variant_id_is_unique(plan, target)
        ):
            # Stale race: a re-render (or another repairer) moved the ground
            # under us. Drop the freshly uploaded object so no orphan survives —
            # UNLESS the winner adopted this very key. Poster keys are
            # deterministic per (job, source), so two concurrent repairs of the
            # same source upload identical bytes to the SAME key: deleting it
            # here would strand the winner's row pointing at a dead object,
            # which is exactly the failure this feature exists to remove.
            if not _references_poster_key(job, target, poster_key):
                delete_object_best_effort(poster_key)
            return "stale_race"

        if target.kind == "job_clip":
            # populate_existing: `_ready_clips` already read this row UNLOCKED
            # into this session's identity map, so a bare locked re-read returns
            # the cached pre-lock attributes — the lock is real, the data is not
            # (tests/test_row_lock_policy.py). Without the refresh the checks
            # below compare stale values to themselves and a concurrent render's
            # thumbnail could be clobbered.
            clip = db.get(JobClip, target.clip_id, with_for_update=True, populate_existing=True)
            if (
                clip is None
                or clip.job_id != jid
                or clip.render_status != "ready"
                or clip.thumbnail_path != target.poster_value
                or _owned_poster(job, clip.video_path) != target.source_key
            ):
                if clip is None or _owned_poster(job, clip.thumbnail_path) != poster_key:
                    delete_object_best_effort(poster_key)
                return "stale_race"
            clip.thumbnail_path = poster_key
        elif target.kind == "job_output":
            plan[target.poster_field] = poster_key
        else:
            # Located by `variant_id` (uniqueness asserted above) — NEVER by
            # list index, which a concurrent reorder would silently repoint.
            target.variant[target.poster_field] = poster_key

        # Keep the marker bound to this source with a clean slate: a later
        # lifecycle deletion of the poster can then be repaired without an
        # inherited attempt count.
        _write_marker(
            plan,
            video_path=target.source_key,
            attempts=0,
            terminal=None,
            enqueued_at=_marker_of(plan).get("enqueued_at"),
        )
        _stage_plan(job, plan)
        db.commit()
        return "generated"


def _references_poster_key(job: Any, target: _RepairTarget | None, poster_key: str) -> bool:
    """True when the row already points at exactly the key we just uploaded.

    Poster keys are deterministic per (job, source), so a concurrent repair of
    the same source writes identical bytes to the same key. When the winner has
    adopted it, the loser must leave the object alone.
    """
    if target is None:
        return False
    return _owned_poster(job, target.poster_value) == poster_key


def _variant_id_is_unique(plan: dict[str, Any] | None, target: _RepairTarget) -> bool:
    """Reject duplicate ``variant_id`` rows rather than mutating the first one."""
    if plan is None or target.kind != "variant" or not target.variant_id:
        return True
    variants = plan.get("variants")
    if not isinstance(variants, list):
        return False
    matches = [
        variant
        for variant in variants
        if isinstance(variant, dict) and str(variant.get("variant_id") or "") == target.variant_id
    ]
    return len(matches) == 1
