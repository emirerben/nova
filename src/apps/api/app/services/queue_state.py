"""Live introspection of Celery workers + Redis queue depth.

Three responsibilities, none of which touch the Job table directly:

  1. `get_live_job_index(celery_app)` — what jobs are active or reserved
     across all live Celery workers, keyed by job_id. Reused by both the
     reaper (the old `_live_job_ids` lived here) and the admin
     job-detail page (`runtime` block on /admin/jobs/{id}/debug).

  2. `get_job_runtime_state(celery_app, job_id)` — one job's runtime
     state: active on worker-X / reserved in queue Q / not found
     (worker probably dead) / unknown (broker unreachable). Powers the
     admin Worker state panel.

  3. `get_queue_snapshot(celery_app)` — broker-level queue depths via
     Redis LLEN, list of active workers, oldest queued job per queue.
     Powers the admin queue summary panel.

Why all three live here: the reaper already proved Celery introspection
works in this codebase. Keeping the "what counts as live?" definition in
ONE module prevents future drift where the reaper and the admin UI
disagree about which jobs are alive.

Failure modes:
  - inspect() returns None (broker hiccup, no workers) → state='unknown'.
    The admin UI must render "unknown" differently from "not_found".
    Claiming "not_found" when we couldn't ask the broker would let an
    operator cancel a healthy job.
  - Redis LLEN raises → empty snapshot, no rows. The list page handles
    a missing summary by showing only what it knows.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Literal

import structlog
from celery import Celery

log = structlog.get_logger()

# Generous timeout — the broker can be slow under load and we'd rather wait
# than skip a sweep. Matches the reaper's historic constant.
_INSPECT_TIMEOUT_S = 5

# Cap how many queued tasks we decode when computing oldest_pending_job_id
# / queue position. Deeper-than-this queues are pathological and the
# admin UI just shows "100+".
_QUEUE_SCAN_CAP = 100

RuntimeStateLiteral = Literal["active", "reserved", "not_found", "unknown"]


@dataclass
class LiveJobIndex:
    """All jobs currently held by Celery workers.

    `active` and `reserved` map job_id → worker hostname (e.g.
    'celery@worker-1'). A job can only appear in one of the two for a
    given worker; both dicts are populated by walking
    `inspect().active()` + `inspect().reserved()`.

    `ok=False` means `inspect()` failed — callers should treat this as
    "unknown" rather than "no jobs are live, reap them all".
    """

    active: dict[str, str] = field(default_factory=dict)
    reserved: dict[str, str] = field(default_factory=dict)
    workers: list[str] = field(default_factory=list)
    ok: bool = True

    def all_job_ids(self) -> set[str]:
        return set(self.active.keys()) | set(self.reserved.keys())


@dataclass
class JobRuntimeState:
    state: RuntimeStateLiteral
    worker: str | None
    task_id: str | None


@dataclass
class QueueInfo:
    name: str
    depth: int
    oldest_pending_job_id: str | None


@dataclass
class QueueSnapshot:
    queues: list[QueueInfo]
    active_workers: list[str]
    ok: bool


def get_live_job_index(celery_app: Celery) -> LiveJobIndex:
    """Walk Celery's inspect() output once, return a job_id-keyed index.

    Convention: orchestrator tasks always pass `job_id` as the first
    positional arg. This module relies on that — see
    `app.services.job_dispatch.enqueue_orchestrator`.

    Returns LiveJobIndex with ok=False on inspect() failure. The reaper
    treats ok=False as "skip this cycle"; admin endpoints treat it as
    "unknown".
    """
    try:
        inspector = celery_app.control.inspect(timeout=_INSPECT_TIMEOUT_S)
        active = inspector.active() or {}
        reserved = inspector.reserved() or {}
        ping = inspector.ping() or {}
    except Exception as exc:  # noqa: BLE001
        log.warning("queue_state_inspect_failed", error=str(exc))
        return LiveJobIndex(ok=False)

    index = LiveJobIndex(workers=sorted(ping.keys()))

    for worker_name, tasks in active.items():
        for task in tasks:
            job_id = _extract_job_id(task)
            if job_id is not None:
                index.active[job_id] = worker_name

    for worker_name, tasks in reserved.items():
        for task in tasks:
            job_id = _extract_job_id(task)
            if job_id is not None:
                index.reserved[job_id] = worker_name

    return index


def get_job_runtime_state(
    celery_app: Celery,
    job_id: str | uuid.UUID,
    celery_task_id: str | None,
) -> JobRuntimeState:
    """One job's runtime state, with `unknown` distinguished from `not_found`.

    `celery_task_id` is what the Job row stores (= str(job_id) by
    convention). When NULL (legacy rows pre-0027), we fall back to
    matching by args[0] in the inspect() output.
    """
    job_id_str = str(job_id)
    task_id = celery_task_id or job_id_str

    index = get_live_job_index(celery_app)
    if not index.ok:
        return JobRuntimeState(state="unknown", worker=None, task_id=task_id)

    # Walk again (the index is keyed by args[0], not by Celery task_id).
    # In practice args[0] == task_id for orchestrators that went through
    # enqueue_orchestrator, but this stays correct for legacy rows too.
    if job_id_str in index.active:
        return JobRuntimeState(
            state="active",
            worker=index.active[job_id_str],
            task_id=task_id,
        )
    if job_id_str in index.reserved:
        return JobRuntimeState(
            state="reserved",
            worker=index.reserved[job_id_str],
            task_id=task_id,
        )
    return JobRuntimeState(state="not_found", worker=None, task_id=task_id)


def get_queue_snapshot(celery_app: Celery) -> QueueSnapshot:
    """Broker-level state: queue depth + workers + oldest queued job per queue.

    Uses Redis LLEN/LRANGE directly via the broker connection (Celery
    doesn't expose queue depth through control inspection — only what's
    reserved on workers). Falls back to ok=False on any failure.
    """
    try:
        # Pull workers from inspect() so we don't issue two separate
        # broker calls for the same data.
        inspector = celery_app.control.inspect(timeout=_INSPECT_TIMEOUT_S)
        ping = inspector.ping() or {}
        active_queues = inspector.active_queues() or {}
    except Exception as exc:  # noqa: BLE001
        log.warning("queue_snapshot_inspect_failed", error=str(exc))
        return QueueSnapshot(queues=[], active_workers=[], ok=False)

    # Distinct queue names across all active workers. Always includes
    # the default queue (`celery`) even if no worker is bound to it.
    queue_names: set[str] = {"celery"}
    for queues in active_queues.values():
        for q in queues:
            name = q.get("name") if isinstance(q, dict) else None
            if name:
                queue_names.add(name)

    queues_out: list[QueueInfo] = []
    try:
        with celery_app.connection_or_acquire() as conn:
            redis_client = conn.default_channel.client  # type: ignore[attr-defined]
            for name in sorted(queue_names):
                try:
                    depth = int(redis_client.llen(name) or 0)
                except Exception as exc:  # noqa: BLE001
                    log.warning("queue_snapshot_llen_failed", queue=name, error=str(exc))
                    continue

                oldest_job_id: str | None = None
                if depth > 0:
                    try:
                        # Redis lists used by Celery are RPUSH/LPOP — head is the
                        # oldest. Decoding the message format is brittle, so we
                        # try and tolerate failure.
                        sample = redis_client.lrange(name, 0, _QUEUE_SCAN_CAP - 1) or []
                        if sample:
                            oldest_job_id = _extract_job_id_from_broker_message(sample[0])
                    except Exception as exc:  # noqa: BLE001
                        log.warning(
                            "queue_snapshot_lrange_failed",
                            queue=name,
                            error=str(exc),
                        )

                queues_out.append(
                    QueueInfo(name=name, depth=depth, oldest_pending_job_id=oldest_job_id)
                )
    except Exception as exc:  # noqa: BLE001
        log.warning("queue_snapshot_redis_failed", error=str(exc))
        return QueueSnapshot(queues=[], active_workers=sorted(ping.keys()), ok=False)

    return QueueSnapshot(queues=queues_out, active_workers=sorted(ping.keys()), ok=True)


def get_queue_position(
    celery_app: Celery,
    job_id: str | uuid.UUID,
    queue_name: str = "celery",
) -> int | None:
    """Index of a queued job in its broker list. None if not in the queue.

    0 = next up. Caps the scan at _QUEUE_SCAN_CAP — if the queue is
    deeper than that, returns None and the UI shows "100+".
    """
    job_id_str = str(job_id)
    try:
        with celery_app.connection_or_acquire() as conn:
            redis_client = conn.default_channel.client  # type: ignore[attr-defined]
            messages = redis_client.lrange(queue_name, 0, _QUEUE_SCAN_CAP - 1) or []
    except Exception as exc:  # noqa: BLE001
        log.warning("queue_position_lookup_failed", queue=queue_name, error=str(exc))
        return None

    for i, msg in enumerate(messages):
        if _extract_job_id_from_broker_message(msg) == job_id_str:
            return i
    return None


# ── internal helpers ─────────────────────────────────────────────────────────


def _extract_job_id(task: dict) -> str | None:
    """First positional arg of the task body, normalized to str. None if absent."""
    args = task.get("args") or []
    if not args:
        return None
    return str(args[0])


def _extract_job_id_from_broker_message(raw: object) -> str | None:
    """Best-effort decode of a Redis-broker Celery message → job_id (args[0]).

    Celery's Redis broker stores tasks as JSON envelopes with a base64-encoded
    body containing `[args, kwargs, embed]`. We decode just enough to pull
    args[0]. Any decode failure returns None — the UI degrades gracefully.
    """
    if raw is None:
        return None
    try:
        import base64  # noqa: PLC0415
        import json  # noqa: PLC0415

        envelope = json.loads(raw if isinstance(raw, (str, bytes, bytearray)) else str(raw))
        body_b64 = envelope.get("body")
        if not body_b64:
            return None
        body_bytes = base64.b64decode(body_b64)
        body = json.loads(body_bytes)
        # body is [args, kwargs, embed]
        if isinstance(body, list) and body and isinstance(body[0], list) and body[0]:
            return str(body[0][0])
    except Exception:  # noqa: BLE001
        return None
    return None
