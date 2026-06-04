"""Stale-build_task reaper (autonomous dev loop, M4 — sibling to reaper.py).

A `build_task` left `in_progress` longer than the threshold means the builder
run that claimed it died mid-task (the GH Actions runner was killed / OOM'd /
hit the 6h Actions cap mid-chunk) without releasing the row. Without a sweep
that row wedges forever — `claim_next_task` only hands out `queued` rows, so a
zombie `in_progress` task is never resumed and never finished.

This sweep resets stale rows back to `queued` (resumable next cron tick) and,
critically, bumps `attempt_count` — so a task that keeps killing its runner
(or is genuinely impossible) eventually trips the cap and goes `blocked`
instead of looping forever. That dual property — "can't wedge forever AND
can't loop forever" — is the whole point (CEO/Eng D3 resilience suite).

Why a generous threshold (and how we "cross-check live runs"): unlike the job
reaper, the builder doesn't run on Celery, so there's no `inspect()` to ask
"is a worker still on this?". The cross-check is purely temporal: a LIVE run
re-stamps the task on every checkpoint (each builder tick), so its `claimed_at`
stays fresh; only a run that has genuinely stopped touching the row lets
`claimed_at` age past the threshold. STALE_THRESHOLD_MIN is deliberately well
beyond a single timeout-bounded run (~15 min) so an in-flight chunk is never
reaped out from under itself.

Modeled on `reaper.py` — same "best-effort sweep, never break a job, generous
threshold" discipline. All status SQL goes through `build_task_repo`.
"""

from __future__ import annotations

import structlog

from app.database import sync_session
from app.services import build_task_repo
from app.services.build_task_repo import ATTEMPT_CAP, STALE_THRESHOLD_MIN

log = structlog.get_logger()


def reap_stale_build_tasks(
    *,
    threshold_min: int = STALE_THRESHOLD_MIN,
    attempt_cap: int = ATTEMPT_CAP,
) -> dict[str, int]:
    """Reset every stale in_progress build_task. Returns a summary count dict.

    Each stale row is reaped via `build_task_repo.reap_stale_task`, which bumps
    attempt_count and routes to `queued` (resumable) or `blocked` (cap tripped).
    Commits once after sweeping all rows (a single short transaction). Safe to
    call repeatedly — a row already past the cap goes `blocked` and is no longer
    `in_progress`, so it won't be re-swept.

    Returns ``{"requeued": N, "blocked": M, "regated": K, "total": N+M+K}``.

    Two stale classes are swept (both are CLAIMED rows a dead run abandoned):
      - in_progress → the builder run died; reap to `queued` (resume the build).
      - gating      → the GATE run died (Docker OOM mid verify-overlays, host
                       slept); reap to `gating` (re-gate the already-built
                       branch, NOT rebuild it). `requeue_status="gating"`.
    Both bump attempt_count, so a persistently-dying run still escalates to
    `blocked` at the cap. An UNCLAIMED stale `gating` row is NOT swept here —
    it isn't wedged (the next gate tick claims it); the digest surfaces it as a
    dead-gate-tick warning instead.
    """
    requeued = 0
    blocked = 0
    regated = 0
    with sync_session() as db:
        for task in build_task_repo.find_stale_in_progress(db, threshold_min=threshold_min):
            result = build_task_repo.reap_stale_task(db, task, attempt_cap=attempt_cap)
            if result == "blocked":
                blocked += 1
            else:
                requeued += 1
        for task in build_task_repo.find_stale_gating(db, threshold_min=threshold_min):
            result = build_task_repo.reap_stale_task(
                db, task, attempt_cap=attempt_cap, requeue_status="gating"
            )
            if result == "blocked":
                blocked += 1
            else:
                regated += 1
        db.commit()

    total = requeued + blocked + regated
    if total:
        log.info(
            "build_task_reaper_swept",
            requeued=requeued,
            blocked=blocked,
            regated=regated,
            threshold_min=threshold_min,
        )
    return {"requeued": requeued, "blocked": blocked, "regated": regated, "total": total}
