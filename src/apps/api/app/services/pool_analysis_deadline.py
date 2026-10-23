"""The declared time bound for one pool-asset (Visuals photo/video) analysis.

KRI-820: a hard-killed `analyze_pool_asset` (time-limit kill, SIGKILL, deploy)
used to leave its row `analyzing` until the maintenance reaper requeued it up
to three times on 10-/60-minute staleness windows, so one stalled JPEG could
read "Analyzing..." for 30-90+ minutes with no creator recovery.

Every attempt now carries one durable deadline (`analysis_deadline_at`):

- dispatch stamps ``now + claim_window(kind) + run_window(kind)``;
- the worker's claim may only shorten it to ``claim_time + run_window(kind)``;
- nothing ever extends it (Celery retries, broker re-sends and the reaper keep it).

Past the deadline an in-flight row is an actionable, retryable
``analysis_timed_out`` failure everywhere at once: the read projection, the
retry route, the reaper (which persists it and rotates the attempt token so a
late worker cannot write) and creator preparation (which leaves it out).

Pure functions only (no I/O; `queue_backlog_stmt` only builds a query) so
routes, tasks and services share one rule.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

# One claimed run: the task's 300 s hard limit (`_AUTOPLACE_TASK_LIMITS`) plus a
# 60 s margin for the persist transaction. Video adds the 360 s budget its
# transient-provider retries historically had.
_RUN_WINDOW = {"image": timedelta(seconds=360), "video": timedelta(seconds=720)}
# Waiting in the broker before a worker claims the row. The base covers an
# autostopped autoplace machine cold-starting plus a short line; the window then
# grows with the analyses already ahead of this one on the shared
# visuals-analysis queue (all creators), one worst-case run per wave of the
# autoplace process's concurrency, so healthy work that is only waiting in line
# never becomes a user-facing timeout. Capped at the pre-KRI-820 60-minute
# queued window so a lost broker message still terminates. Only the run window
# after a claim is tight.
_CLAIM_WINDOW = {"image": timedelta(minutes=10), "video": timedelta(minutes=30)}
_CLAIM_WINDOW_CAP = timedelta(minutes=60)
# `autoplace` process in fly.toml: `--concurrency=2 -Q autoplace-jobs,visuals-analysis`.
_QUEUE_CONCURRENCY = 2
# One queued analysis ahead occupies a slot for at most the task's 300 s hard
# limit (image) or one full video run window.
_QUEUE_SLOT = {"image": timedelta(seconds=300), "video": timedelta(seconds=720)}

IN_FLIGHT_STATUSES = frozenset({"queued", "analyzing"})
TIMED_OUT_CODE = "analysis_timed_out"
TIMED_OUT_DETAIL = "Kria couldn't finish analyzing this file in time. Retry or remove it."


def _kind(kind: str | None) -> str:
    return "video" if kind == "video" else "image"


def run_window(kind: str | None) -> timedelta:
    return _RUN_WINDOW[_kind(kind)]


def claim_window(kind: str | None, queue_ahead: int = 0) -> timedelta:
    """Time a dispatched attempt may wait for a worker, given how many other
    in-flight analyses are already ahead of it on the shared queue."""
    k = _kind(kind)
    waves = -(-max(0, int(queue_ahead)) // _QUEUE_CONCURRENCY)
    backlog = _QUEUE_SLOT[k] * waves
    return min(max(_CLAIM_WINDOW[k], backlog + _CLAIM_WINDOW["image"]), _CLAIM_WINDOW_CAP)


def max_dispatch_window() -> timedelta:
    """Longest any in-flight analysis can stay pending (used by waiters)."""
    return max(_CLAIM_WINDOW_CAP + run_window(k) for k in _RUN_WINDOW)


def dispatch_deadline(kind: str | None, now: datetime, queue_ahead: int = 0) -> datetime:
    return now + claim_window(kind, queue_ahead) + run_window(kind)


def queue_backlog_stmt(now: datetime, *, exclude_id: Any = None):
    """COUNT of live in-flight pool analyses (served by the partial
    `idx_plan_item_assets_in_flight_deadline` index). Built here, executed by
    the caller (sync or async), so every dispatch site sizes the window alike."""
    from sqlalchemy import func, or_, select  # noqa: PLC0415

    from app.models import PlanItemAsset  # noqa: PLC0415

    stmt = (
        select(func.count())
        .select_from(PlanItemAsset)
        .where(
            PlanItemAsset.status.in_(tuple(IN_FLIGHT_STATUSES)),
            or_(
                PlanItemAsset.analysis_deadline_at.is_(None),
                PlanItemAsset.analysis_deadline_at > now,
            ),
        )
    )
    if exclude_id is not None:
        stmt = stmt.where(PlanItemAsset.id != exclude_id)
    return stmt


def claim_deadline(current: datetime | None, kind: str | None, now: datetime) -> datetime:
    """A claim may shorten the bound to one run, never extend it."""
    capped = now + run_window(kind)
    return capped if current is None else min(current, capped)


def effective_deadline(asset: Any) -> datetime | None:
    """The row's deadline; legacy in-flight rows (pre-0125) derive one."""
    explicit = getattr(asset, "analysis_deadline_at", None)
    dispatched = getattr(asset, "analysis_last_dispatched_at", None)
    # A deadline older than the attempt's dispatch belongs to an earlier token
    # (a requeue by pre-KRI-820 code): derive a fresh one instead.
    if explicit is not None and (dispatched is None or explicit > dispatched):
        return explicit
    status = getattr(asset, "status", None)
    kind = getattr(asset, "kind", None)
    if status == "analyzing":
        anchor = getattr(asset, "analysis_started_at", None) or getattr(asset, "created_at", None)
        return anchor + run_window(kind) if anchor is not None else None
    if status == "queued":
        anchor = getattr(asset, "analysis_last_dispatched_at", None) or getattr(
            asset, "created_at", None
        )
        # Its backlog at dispatch is unknown: give it the full capped window.
        return anchor + _CLAIM_WINDOW_CAP + run_window(kind) if anchor is not None else None
    return None


def is_expired(asset: Any, now: datetime | None = None) -> bool:
    """In flight past its deadline. Ready/failed/refresh rows never expire."""
    if getattr(asset, "status", None) not in IN_FLIGHT_STATUSES:
        return False
    deadline = effective_deadline(asset)
    return deadline is not None and (now or datetime.now(UTC)) > deadline


def begin_attempt(asset: Any, *, now: datetime, attempt_count: int, queue_ahead: int = 0) -> str:
    """Stamp a fresh fenced `queued` attempt with its deadline; return the token.

    `queue_ahead` is the live in-flight backlog (`queue_backlog_stmt`) so the
    claim window covers the line this attempt joins."""
    token = uuid.uuid4().hex
    asset.status = "queued"
    asset.error_code = None
    asset.error_detail = None
    asset.error_retryable = False
    asset.analysis_attempt_token = token
    asset.analysis_attempt_count = attempt_count
    asset.analysis_last_dispatched_at = now
    asset.analysis_started_at = None
    asset.analysis_deadline_at = dispatch_deadline(getattr(asset, "kind", None), now, queue_ahead)
    return token


def mark_timed_out(asset: Any) -> None:
    """Terminal, retryable timeout. Rotating the token fences every late worker
    and stale broker copy of the expired attempt."""
    asset.status = "failed"
    asset.error_code = TIMED_OUT_CODE
    asset.error_detail = TIMED_OUT_DETAIL
    asset.error_retryable = True
    asset.analysis_attempt_token = uuid.uuid4().hex
    asset.analysis_started_at = None
    if getattr(asset, "media_status", None) == "pending":
        # Bytes were never verified by this attempt; a verified ("ready") photo
        # stays manually usable even though its AI analysis timed out.
        asset.media_status = "failed"
