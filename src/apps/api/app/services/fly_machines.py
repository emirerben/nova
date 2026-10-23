"""Thin Fly.io Machines API client for the render-worker autostop lifecycle.

Resolves the target machine by `config.metadata.fly_process_group == "worker"`
(confirmed shape via a live `GET /v1/apps/{app}/machines` call against prod —
Fly stamps this automatically from fly.toml's `[processes]` names), NOT a
hardcoded machine ID — the ID changes every time the machine is recreated (a
deploy, a resize, a crash-restart).

Every public function is fail-soft: on any error (network, auth, unexpected
API shape, ambiguous machine resolution) it logs and returns a null/False
result rather than raising. Both callers — the `before_task_publish` wake-hook
signal in worker.py, and the `tasks.manage_render_worker_lifecycle` Beat task
in maintenance.py — have their own bounded-worst-case backstop (the periodic
lifecycle tick re-checks and retries), so a failed call here is recoverable,
not catastrophic. This mirrors the reaper's "don't act on unknown" philosophy
from the opposite direction: here, the action already failed to happen, and
doing nothing further is the safe default (never leaves the render worker in
a half-stopped or wrongly-stopped state by guessing).

Fail-CLOSED exception: when machine resolution finds zero or 2+ candidates —
states that should never occur in normal operation — this module refuses to
guess. Zero matches logs loud (misconfigured FLY_APP_NAME, or the process
group was renamed without updating this module). 2+ matches logs loud and
acts on NONE of them (a bad deploy that left two `worker` machines is exactly
the scenario where guessing wrong means silently starting/stopping the WRONG
one).
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import structlog

from app.config import settings

log = structlog.get_logger()

_API_BASE = "https://api.machines.dev/v1"

# Connect must fail fast — a network black hole must not hang the caller
# (the wake hook fires from a background thread off a FastAPI request
# handler; the lifecycle task runs on a Beat tick). The overall request
# timeout is a little longer since Fly's start/stop actions can take a beat
# to process, but still bounded well under the ~2min lifecycle poll interval
# that would otherwise cover for a missed/slow call.
_TIMEOUT = httpx.Timeout(5.0, connect=2.0)

WORKER_PROCESS_GROUP = "worker"


def _headers() -> dict[str, str] | None:
    """Auth header, or None if FLY_API_TOKEN isn't configured.

    Never raises — callers treat None as "can't reach Fly right now",
    identical to any other failure mode.
    """
    if not settings.FLY_API_TOKEN:
        log.warning("fly_machines_no_api_token_configured")
        return None
    return {"Authorization": f"Bearer {settings.FLY_API_TOKEN}"}


def _list_machines() -> list[dict[str, Any]] | None:
    headers = _headers()
    if headers is None:
        return None
    url = f"{_API_BASE}/apps/{settings.FLY_APP_NAME}/machines"
    try:
        response = httpx.get(url, headers=headers, timeout=_TIMEOUT)
        response.raise_for_status()
        data = response.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("fly_machines_list_failed", error=str(exc))
        return None
    if not isinstance(data, list):
        log.warning("fly_machines_list_unexpected_shape", data_type=type(data).__name__)
        return None
    return data


def _resolve_worker_machine(
    machines: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Find the single machine whose metadata.fly_process_group == "worker".

    Returns None (loud-logged) on zero or 2+ matches — see module docstring.
    """
    matches = [
        m
        for m in machines
        if isinstance(m, dict)
        and (m.get("config") or {}).get("metadata", {}).get("fly_process_group")
        == WORKER_PROCESS_GROUP
    ]
    if not matches:
        log.error(
            "fly_machines_worker_not_found",
            app=settings.FLY_APP_NAME,
            process_group=WORKER_PROCESS_GROUP,
            total_machines=len(machines),
        )
        return None
    if len(matches) > 1:
        log.error(
            "fly_machines_worker_ambiguous",
            app=settings.FLY_APP_NAME,
            process_group=WORKER_PROCESS_GROUP,
            candidate_ids=[m.get("id") for m in matches],
        )
        return None
    return matches[0]


def get_render_worker_state() -> str | None:
    """The render worker machine's current Fly `state` field, or None.

    None covers every failure mode uniformly: no token configured, network
    failure, zero/ambiguous machine resolution. Callers must treat None as
    "unknown" — never infer "started" or "stopped" from a missing answer.

    States observed in practice go beyond a simple started/stopped binary
    (e.g. "starting", "stopping", "replacing" during a deploy) — callers
    that only care about "is it definitely started" should check
    `state == "started"` explicitly rather than `state != "stopped"`.
    """
    machines = _list_machines()
    if machines is None:
        return None
    machine = _resolve_worker_machine(machines)
    if machine is None:
        return None
    state = machine.get("state")
    return state if isinstance(state, str) else None


def _resolve_worker() -> dict[str, Any] | None:
    machines = _list_machines()
    if machines is None:
        return None
    machine = _resolve_worker_machine(machines)
    if machine is None:
        return None
    if not machine.get("id"):
        log.error("fly_machines_worker_missing_id", machine=machine)
        return None
    return machine


def _post_lifecycle_action(machine_id: str, action: str) -> httpx.Response | None:
    """Return the Fly response, or None when no request could complete."""
    headers = _headers()
    if headers is None:
        return None
    url = f"{_API_BASE}/apps/{settings.FLY_APP_NAME}/machines/{machine_id}/{action}"
    try:
        return httpx.post(url, headers=headers, timeout=_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        log.warning(f"fly_machines_{action}_failed", machine_id=machine_id, error=str(exc))
        return None


# Keep one long poll below the lifecycle task's time limit. This is our cap,
# not a Fly platform limit. Each subsequent attempt re-resolves the worker.
_WAIT_CALL_MAX_S = 60.0
_START_RETRY_BACKOFF_S = 5.0
_START_MAX_ATTEMPTS = 12


def _wait_for_stopped(machine: dict[str, Any], budget_s: float) -> None:
    """Wait for this machine instance, with bounded backoff on failed waits."""
    deadline = time.monotonic() + budget_s

    def backoff() -> None:
        remaining = max(0.0, deadline - time.monotonic())
        if remaining:
            time.sleep(min(remaining, _START_RETRY_BACKOFF_S))

    headers = _headers()
    instance_id = machine.get("instance_id")
    if headers is None or not instance_id or budget_s < 1:
        backoff()
        return
    timeout_s = int(min(budget_s, _WAIT_CALL_MAX_S))
    try:
        response = httpx.get(
            f"{_API_BASE}/apps/{settings.FLY_APP_NAME}/machines/{machine['id']}/wait",
            headers=headers,
            params={"state": "stopped", "instance_id": instance_id, "timeout": timeout_s},
            timeout=httpx.Timeout(timeout_s, connect=min(2.0, timeout_s)),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("fly_machines_wait_failed", machine_id=machine["id"], error=str(exc))
        backoff()
        return
    if response.status_code not in (200, 408):
        log.warning(
            "fly_machines_wait_unexpected_status",
            machine_id=machine["id"],
            status=response.status_code,
        )
        backoff()


def start_render_worker(wait_s: float = 0.0) -> bool:
    """Start the worker, retrying a stopping/412 race within a bounded window.

    Zero preserves one-attempt behavior. The positive retry window includes
    sleeps and waits; an already-running list/start request can finish after
    it, bounded by _TIMEOUT. The lifecycle reserves additional time for these
    requests under its 100-second soft limit. Unknown/ambiguous workers and
    non-412 start failures never trigger speculative starts or retries.
    """
    deadline = time.monotonic() + max(0.0, wait_s)
    for attempt in range(1, _START_MAX_ATTEMPTS + 1):
        if attempt > 1 and time.monotonic() >= deadline:
            break
        machine = _resolve_worker()
        if machine is None:
            return False
        remaining = deadline - time.monotonic()
        if wait_s > 0 and remaining <= 0:
            break
        machine_id = str(machine["id"])
        if machine.get("state") == "stopping":
            if remaining <= 0:
                break
            log.info("fly_machines_start_waiting_for_stop", machine_id=machine_id, attempt=attempt)
            _wait_for_stopped(machine, remaining)
            continue
        response = _post_lifecycle_action(machine_id, "start")
        if response is not None and 200 <= response.status_code < 300:
            log.info("fly_machines_start_ok", machine_id=machine_id, attempt=attempt)
            return True
        remaining = deadline - time.monotonic()
        if response is None or response.status_code != 412 or remaining <= 0:
            log.warning(
                "fly_machines_start_failed",
                machine_id=machine_id,
                status=response.status_code if response is not None else None,
            )
            return False
        log.info(
            "fly_machines_start_precondition_failed_retrying",
            machine_id=machine_id,
            attempt=attempt,
        )
        time.sleep(min(remaining, _START_RETRY_BACKOFF_S))
    log.warning("fly_machines_start_failed", error="retry window or attempts exhausted")
    return False


def stop_render_worker() -> bool:
    """Ask Fly to stop the render worker machine. True on success.

    Callers (tasks.manage_render_worker_lifecycle) MUST have already
    confirmed render_worker_idle() via app.services.queue_state before
    calling this — this function does not re-check idleness itself, it
    only performs the Fly API call.
    """
    machine = _resolve_worker()
    if machine is None:
        return False
    response = _post_lifecycle_action(str(machine["id"]), "stop")
    ok = response is not None and 200 <= response.status_code < 300
    if ok:
        log.info("fly_machines_stop_ok", machine_id=machine["id"])
    else:
        log.warning(
            "fly_machines_stop_failed",
            machine_id=machine["id"],
            status=response.status_code if response is not None else None,
        )
    return ok
