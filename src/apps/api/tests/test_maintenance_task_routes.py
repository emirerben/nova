"""Regression guard: no dispatch site overrides task_routes for a
maintenance task.

Celery's dispatch-time `queue=` kwarg on `.apply_async()`/`.delay()`
OVERRIDES `task_routes` (verified empirically — a task explicitly
dispatched with `queue="celery"` ignores its task_routes entry entirely).
Since all of these tasks are Beat-scheduled (dispatched by Celery Beat
itself, not application code), the risk is narrow, but `cleanup_cancelled_job`
IS dispatched from application code (an admin cancel action) — if a future
edit there (or anywhere) adds an explicit `queue=` kwarg to one of these
calls, it would silently defeat the render-worker autostop split (Phase 1)
by routing a maintenance task back onto the render worker's queues, which
might be stopped. Mirrors the AST-avoidance style of
tests/services/test_job_dispatch.py's regression guard, but via a plain
source grep — this codebase doesn't currently have any conflicting call
sites, and this test exists to keep it that way.
"""

from __future__ import annotations

import re
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[1] / "app"

# Python identifiers for each name in app.worker.MAINTENANCE_TASK_NAMES.
# Celery task `name=` strings and Python function/variable names differ in
# this codebase (e.g. celery name "tasks.sweep_stale_jobs" vs. Python
# identifier `sweep_stale_jobs`) — this maps the ones with dispatch call
# sites reachable from application code (not just Beat's own scheduler).
_MAINTENANCE_PYTHON_IDENTIFIERS: tuple[str, ...] = (
    "sweep_stale_jobs",
    "cleanup_agent_runs",
    "send_daily_digest",
    "cleanup_cancelled_job",
    "purge_job_storage",
    "sweep_job_storage_deletions",
    "poll_tiktok_publications",
    "schedule_tiktok_account_syncs",
    "cleanup_tiktok_publications",
    "manage_render_worker_lifecycle",
)


def test_no_dispatch_site_overrides_maintenance_task_routing() -> None:
    offenders: list[str] = []

    for path in sorted(API_ROOT.rglob("*.py")):
        if any(part in path.parts for part in ("migrations", "__pycache__")):
            continue

        text = path.read_text(encoding="utf-8")

        for name in _MAINTENANCE_PYTHON_IDENTIFIERS:
            for match in re.finditer(rf"\b{name}\.(delay|apply_async)\(", text):
                # Look at the ~200 chars following the call for an explicit
                # queue= kwarg — a cheap heuristic, matching the same
                # approach test_job_dispatch.py already uses for a
                # structurally identical check.
                snippet = text[match.end() : match.end() + 200]
                if "queue=" in snippet:
                    rel = path.relative_to(API_ROOT.parent)
                    offenders.append(f"{rel}:{match.start()} → {name}.{match.group(1)}(queue=...)")

    assert not offenders, (
        "These maintenance-task dispatch sites pass an explicit queue= kwarg, "
        "which OVERRIDES task_routes and would silently route the task back "
        "onto the render worker's queues (which may be stopped under "
        "RENDER_AUTOSTOP_ENABLED):\n  " + "\n  ".join(offenders)
    )


def test_maintenance_task_names_have_no_explicit_queue_kwarg_in_beat_schedule() -> None:
    """Belt-and-suspenders: confirm the beat_schedule entries themselves
    don't carry a conflicting `options: {"queue": ...}` override either —
    Celery Beat schedule entries support this and it would have the same
    effect as a dispatch-time queue= kwarg."""
    from app.worker import celery_app

    for beat_name, entry in celery_app.conf.beat_schedule.items():
        options = entry.get("options") or {}
        queue_override = options.get("queue")
        assert queue_override in (None, "maintenance"), (
            f"beat_schedule entry {beat_name!r} sets options.queue="
            f"{queue_override!r}, which conflicts with task_routes routing "
            f"it to 'maintenance'"
        )


def test_poster_repair_queue_is_a_property_of_the_task_not_the_dispatcher() -> None:
    """A bare `repair_job_poster.delay(...)` must not reach the render worker.

    Without this `task_routes` entry a future dispatcher that forgets `queue=`
    falls back to the default `celery` queue — the concurrency=1 render worker
    — and head-of-line-blocks renders behind a full-MP4 download.

    It must also stay OUT of the blanket maintenance rule: that lane is the 1GB
    `light`/Beat machine, and this task downloads a full MP4 into RAM-backed
    /tmp — exactly the workload of the 2026-08-02 OOM.
    """
    from app.config import settings
    from app.worker import MAINTENANCE_TASK_NAMES, celery_app

    route = celery_app.conf.task_routes.get("tasks.repair_job_poster")

    assert route == {"queue": settings.poster_repair_queue}
    assert "tasks.repair_job_poster" not in MAINTENANCE_TASK_NAMES
    assert route["queue"] != "maintenance"
