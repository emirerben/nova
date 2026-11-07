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
# Waiting in the broker before a worker claims the row: a cold start plus a
# short line (image 10 min; video 30 min), plus the lane already busy with one
# other task, plus the analyses already ahead on the shared visuals-analysis
# queue (all creators). Healthy work that is only waiting in line must never
# become a user-facing timeout: the reaper fails an expired `queued` row
# without asking the broker whether its message is still there. Capped so a
# lost broker message still terminates. Only the run window after a claim is
# tight.
_CLAIM_WINDOW = {"image": timedelta(minutes=10), "video": timedelta(minutes=30)}
# Large enough for 14 videos queued behind a busy lane (30 + 40 + 13 x 13 min
# = 239 min); a lost message on a quiet lane still ends after base + occupant.
_CLAIM_WINDOW_CAP = timedelta(hours=4)
# Budget only the consumer that is always there: the unified `worker` process
# in fly.toml (`--concurrency=1`), one slot shared with renders, Drive imports
# and footage/speech analysis. A dedicated Visuals group (KRI-1240; #2183
# removes it for cost) can be draining, restarting or gone, and extra
# consumers only finish work sooner. tests/test_pool_asset_fly_contract.py pins this to the
# worker's --concurrency.
_QUEUE_CONCURRENCY = 1
# One analysis ahead holds the lane for at most its run window plus the
# soft-to-hard limit gap (`_AUTOPLACE_TASK_LIMITS` 240/300 s): a retry may
# start just inside its deadline and run to the hard limit. Pinned by
# tests/test_pool_asset_fly_contract.py.
_QUEUE_SLOT = {"image": timedelta(seconds=420), "video": timedelta(seconds=780)}
# The shared lane may already be running one non-analysis task when an
# analysis is dispatched: up to the longest hard time limit of any task the
# worker consumes (`tasks.batch_import_from_drive`, 2400 s; renders 1800 s).
# Pinned >= that maximum by tests/test_pool_asset_fly_contract.py. Not
# covered: further non-analysis tasks interleaved into a burst (Celery cycles
# the worker's queues); the cap and a retryable timeout are the backstop.
_LANE_OCCUPANT = timedelta(seconds=2400)

IN_FLIGHT_STATUSES = frozenset({"queued", "analyzing"})
TIMED_OUT_CODE = "analysis_timed_out"
TIMED_OUT_DETAIL = "Kria couldn't finish analyzing this file in time. Retry or remove it."


def _kind(kind: str | None) -> str:
    return "video" if kind == "video" else "image"


def run_window(kind: str | None) -> timedelta:
    return _RUN_WINDOW[_kind(kind)]


def queue_slot_s(kind: str | None) -> int:
    """Lane seconds one in-flight analysis of this kind can hold the slot."""
    return int(_QUEUE_SLOT[_kind(kind)].total_seconds())


def claim_window(kind: str | None, backlog_s: int = 0) -> timedelta:
    """Time a dispatched attempt may wait for a worker, given the lane seconds
    (`queue_backlog_stmt`) of the in-flight analyses already ahead of it."""
    backlog = timedelta(seconds=max(0, int(backlog_s)) / _QUEUE_CONCURRENCY)
    return min(_CLAIM_WINDOW[_kind(kind)] + _LANE_OCCUPANT + backlog, _CLAIM_WINDOW_CAP)


def max_dispatch_window() -> timedelta:
    """Longest any in-flight analysis can stay pending (used by waiters)."""
    return max(_CLAIM_WINDOW_CAP + run_window(k) for k in _RUN_WINDOW)


def dispatch_deadline(kind: str | None, now: datetime, backlog_s: int = 0) -> datetime:
    return now + claim_window(kind, backlog_s) + run_window(kind)


def queue_backlog_stmt(now: datetime, *, exclude_id: Any = None):
    """Lane seconds (`queue_slot_s` per kind) of the live in-flight pool
    analyses (served by the partial `idx_plan_item_assets_in_flight_deadline`
    index). Built here, executed by the caller (sync or async), so every
    dispatch site sizes the window alike."""
    from sqlalchemy import case, func, or_, select  # noqa: PLC0415

    from app.models import PlanItemAsset  # noqa: PLC0415

    slot = case(
        (PlanItemAsset.kind == "video", queue_slot_s("video")),
        else_=queue_slot_s("image"),
    )
    stmt = (
        select(func.coalesce(func.sum(slot), 0))
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


def begin_attempt(asset: Any, *, now: datetime, attempt_count: int, backlog_s: int = 0) -> str:
    """Stamp a fresh fenced `queued` attempt with its deadline; return the token.

    `backlog_s` is the live in-flight backlog in lane seconds
    (`queue_backlog_stmt`) so the claim window covers the line this attempt
    joins."""
    token = uuid.uuid4().hex
    asset.status = "queued"
    asset.error_code = None
    asset.error_detail = None
    asset.error_retryable = False
    asset.analysis_attempt_token = token
    asset.analysis_attempt_count = attempt_count
    asset.analysis_last_dispatched_at = now
    asset.analysis_started_at = None
    asset.analysis_deadline_at = dispatch_deadline(getattr(asset, "kind", None), now, backlog_s)
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
