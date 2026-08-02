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


def _post_lifecycle_action(action: str) -> bool:
    """POST /machines/{id}/{action} ("start" or "stop"). True on 2xx."""
    machines = _list_machines()
    if machines is None:
        return False
    machine = _resolve_worker_machine(machines)
    if machine is None:
        return False
    machine_id = machine.get("id")
    if not machine_id:
        log.error("fly_machines_worker_missing_id", machine=machine)
        return False

    headers = _headers()
    if headers is None:
        return False
    url = f"{_API_BASE}/apps/{settings.FLY_APP_NAME}/machines/{machine_id}/{action}"
    try:
        response = httpx.post(url, headers=headers, timeout=_TIMEOUT)
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        log.warning(f"fly_machines_{action}_failed", machine_id=machine_id, error=str(exc))
        return False
    log.info(f"fly_machines_{action}_ok", machine_id=machine_id)
    return True


def start_render_worker() -> bool:
    """Ask Fly to start the render worker machine. True on success.

    Idempotent from the caller's point of view: starting an already-started
    machine is a Fly no-op that still returns 2xx. Never raises — see module
    docstring for the fail-soft rationale.
    """
    return _post_lifecycle_action("start")


def stop_render_worker() -> bool:
    """Ask Fly to stop the render worker machine. True on success.

    Callers (tasks.manage_render_worker_lifecycle) MUST have already
    confirmed render_worker_idle() via app.services.queue_state before
    calling this — this function does not re-check idleness itself, it
    only performs the Fly API call.
    """
    return _post_lifecycle_action("stop")
