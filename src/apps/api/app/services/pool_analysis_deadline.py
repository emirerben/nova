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

Pure functions only (no I/O) so routes, tasks and services share one rule.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

# One claimed run: the task's 300 s hard limit (`_AUTOPLACE_TASK_LIMITS`) plus a
# 60 s margin for the persist transaction. Video adds the 360 s budget its
# transient-provider retries historically had.
_RUN_WINDOW = {"image": timedelta(seconds=360), "video": timedelta(seconds=720)}
# Waiting in the broker before a worker claims the row. A 20-photo batch at the
# autoplace worker's concurrency of 2 clears in ~5 min; videos run longer, so a
# full video batch gets a longer claim window rather than failing healthy work.
_CLAIM_WINDOW = {"image": timedelta(minutes=10), "video": timedelta(minutes=30)}

IN_FLIGHT_STATUSES = frozenset({"queued", "analyzing"})
TIMED_OUT_CODE = "analysis_timed_out"
TIMED_OUT_DETAIL = "Kria couldn't finish analyzing this file in time. Retry or remove it."


def _kind(kind: str | None) -> str:
    return "video" if kind == "video" else "image"


def run_window(kind: str | None) -> timedelta:
    return _RUN_WINDOW[_kind(kind)]


def claim_window(kind: str | None) -> timedelta:
    return _CLAIM_WINDOW[_kind(kind)]


def max_dispatch_window() -> timedelta:
    """Longest any in-flight analysis can stay pending (used by waiters)."""
    return max(claim_window(k) + run_window(k) for k in _RUN_WINDOW)


def dispatch_deadline(kind: str | None, now: datetime) -> datetime:
    return now + claim_window(kind) + run_window(kind)


def claim_deadline(current: datetime | None, kind: str | None, now: datetime) -> datetime:
    """A claim may shorten the bound to one run, never extend it."""
    capped = now + run_window(kind)
    return capped if current is None else min(current, capped)


def effective_deadline(asset: Any) -> datetime | None:
    """The row's deadline; legacy in-flight rows (pre-0125) derive one."""
    explicit = getattr(asset, "analysis_deadline_at", None)
    if explicit is not None:
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
        return dispatch_deadline(kind, anchor) if anchor is not None else None
    return None


def is_expired(asset: Any, now: datetime | None = None) -> bool:
    """In flight past its deadline. Ready/failed/refresh rows never expire."""
    if getattr(asset, "status", None) not in IN_FLIGHT_STATUSES:
        return False
    deadline = effective_deadline(asset)
    return deadline is not None and (now or datetime.now(UTC)) > deadline


def begin_attempt(asset: Any, *, now: datetime, attempt_count: int) -> str:
    """Stamp a fresh fenced `queued` attempt with its deadline; return the token."""
    token = uuid.uuid4().hex
    asset.status = "queued"
    asset.error_code = None
    asset.error_detail = None
    asset.error_retryable = False
    asset.analysis_attempt_token = token
    asset.analysis_attempt_count = attempt_count
    asset.analysis_last_dispatched_at = now
    asset.analysis_started_at = None
    asset.analysis_deadline_at = dispatch_deadline(getattr(asset, "kind", None), now)
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
