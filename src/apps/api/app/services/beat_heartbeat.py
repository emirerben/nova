"""Beat-liveness heartbeat — the ONLY mechanism that can detect Celery Beat
being fully dead (Fly cost-cut plan / render-worker-autostop, see
agents/DECISIONS.md). A check that is itself Beat-scheduled shares Beat's
exact blind spot: if Beat's process/machine goes down entirely, nothing
Beat-scheduled fires, including a heartbeat check. This module writes a
Redis timestamp from the `task_success` signal (app/worker.py) on every
Beat-scheduled task's successful completion, and exposes a read-side check
consumed by `GET /health/beat` (app/main.py) — meant to be pinged by a
service that runs OUTSIDE this app entirely (UptimeRobot, cron-job.org, a
scheduled GitHub Actions workflow), since that's the only kind of check
immune to Beat's own death.

Why this specifically matters now: before render-worker autostop, if Beat
died, the render worker kept running and kept consuming jobs normally (it
was never stopped). After autostop, the ONLY thing that restarts a stopped
render worker on a missed wake-hook call is Beat's own lifecycle backstop
task (tasks.manage_render_worker_lifecycle) — so Beat's death now means
every future render sits `queued` forever, silently (the API still returns
200 on job creation). This heartbeat is what makes that failure mode
detectable at all; nothing inside this app can detect it on its own.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime

import structlog

from app.config import settings

log = structlog.get_logger()

_BEAT_HEARTBEAT_KEY = "beat:last_success_at"

_heartbeat_redis_lock = threading.Lock()
_heartbeat_redis_client = None


def _get_heartbeat_redis():
    """Module-singleton Redis client — same per-module pooled pattern used
    throughout this codebase (clip_cache.py, the wake-hook debounce in
    worker.py, the lifecycle idle-tracker in maintenance.py)."""
    global _heartbeat_redis_client
    if _heartbeat_redis_client is not None:
        return _heartbeat_redis_client
    with _heartbeat_redis_lock:
        if _heartbeat_redis_client is not None:
            return _heartbeat_redis_client
        try:
            import redis as redis_lib  # noqa: PLC0415

            client = redis_lib.from_url(
                settings.redis_url, socket_connect_timeout=2, socket_timeout=2
            )
            client.ping()
            _heartbeat_redis_client = client
        except Exception as exc:  # noqa: BLE001
            log.warning("beat_heartbeat_redis_unavailable", error=str(exc))
            return None
    return _heartbeat_redis_client


def record_beat_task_success() -> None:
    """Write 'now' as the last-successful-Beat-task-run timestamp.

    Called from the task_success signal handler in worker.py, already
    filtered to Beat-scheduled task names only (BEAT_SCHEDULED_TASK_NAMES).
    Never raises — best-effort, matching every other signal handler in
    this codebase.
    """
    client = _get_heartbeat_redis()
    if client is None:
        return
    try:
        client.set(_BEAT_HEARTBEAT_KEY, str(datetime.now(UTC).timestamp()))
    except Exception as exc:  # noqa: BLE001
        log.warning("beat_heartbeat_write_failed", error=str(exc))


def beat_heartbeat_status() -> tuple[bool, float | None]:
    """Returns (healthy, age_seconds).

    healthy=False when: no heartbeat has EVER been recorded, the most
    recent one is older than BEAT_HEARTBEAT_STALE_AFTER_MIN minutes, or
    Redis itself is unreachable. "Never recorded" is deliberately treated
    the same as "stale" rather than assumed healthy — a fresh deploy with a
    legitimately-not-yet-fired first tick and an actually-dead Beat look
    identical from this read alone, and reporting unhealthy in that narrow
    window is the safe direction to be wrong in.

    age_seconds is None when there's no reading at all (never recorded, or
    Redis unavailable) — distinct from "saw one, it's just old."
    """
    client = _get_heartbeat_redis()
    if client is None:
        return False, None
    try:
        raw = client.get(_BEAT_HEARTBEAT_KEY)
    except Exception as exc:  # noqa: BLE001
        log.warning("beat_heartbeat_read_failed", error=str(exc))
        return False, None
    if not raw:
        return False, None
    try:
        last_success_at = float(raw)
    except (TypeError, ValueError):
        return False, None
    age_seconds = datetime.now(UTC).timestamp() - last_success_at
    healthy = age_seconds < settings.BEAT_HEARTBEAT_STALE_AFTER_MIN * 60
    return healthy, age_seconds
