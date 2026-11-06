"""Redis side-store for the suggestion agent's picks (KRI-756, AI suggestion blocks).

The suggestion agent runs in a worker, off the request path, and the GET route only ever reads
what it left here. Nothing is written to ``thread.state`` from the worker (the ring write
read-modify-writes the whole state JSON), and there is no migration.

Keys (all TTL 24h, all fail-open: a Redis outage degrades to the deterministic set, never a 500):

* ``kria:suggester:{thread}:{digest}``         agent result (validated picks or a fallback marker)
* ``kria:suggester:served:{thread}:{digest}``  the final set a creator was served: it never
                                              changes for a digest (SET NX; first writer wins)
* ``kria:suggester:seen:{thread}:{digest}``    when a GET first saw the digest with no result
                                              (starts the 6 s agent wait)
* ``kria:suggester:lock:{thread}:{digest}``    single-flight for enqueueing a run
* ``kria:suggester:runs:{thread}``             per-thread run counter
                                              (``KRIA_SUGGESTER_MAX_RUNS_PER_THREAD``)
* ``kria:suggester:hint:{thread}``             language/protocol the GET last saw (the worker
                                              has no request headers)
* ``kria:suggester:debounce:{thread}:-``       one pending freshness refresh per debounce window
                                              (``KRIA_SUGGESTION_EDITOR_TRIGGERS_ENABLED``)

The functions are synchronous; async callers use ``asyncio.to_thread``.
"""

from __future__ import annotations

import json
import time
from typing import Any

import structlog

from app.services.suggestions.models import SuggestionAgentOutput

log = structlog.get_logger()

TTL_S = 24 * 60 * 60
LOCK_TTL_S = 120
RUNS_TTL_S = 7 * 24 * 60 * 60
HINT_TTL_S = 24 * 60 * 60

_client: Any = None


class StoreUnavailable(Exception):  # noqa: N818 - internal control flow
    """Redis could not be reached; callers fall back to the deterministic set."""


def _redis() -> Any:
    global _client  # noqa: PLW0603
    if _client is None:
        import redis  # noqa: PLC0415

        from app.config import settings  # noqa: PLC0415

        _client = redis.from_url(settings.redis_url, socket_connect_timeout=0.5, socket_timeout=0.5)
    return _client


def _key(*parts: str) -> str:
    return ":".join(("kria", "suggester", *parts))


def _loads(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode()
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _guard(fn: Any) -> Any:
    try:
        return fn()
    except StoreUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - every Redis failure is "store unavailable"
        raise StoreUnavailable(str(exc)) from exc


def ok_result(output: SuggestionAgentOutput, *, prompt_version: str, model: str | None) -> dict:
    """The stored shape of a usable agent answer (already validated against what was issued)."""
    return {
        "status": "ok",
        "picks": output.model_dump(mode="json", by_alias=True)["ranked"],
        "prompt_version": prompt_version,
        "model": model,
    }


def fallback_result(reason: str, *, prompt_version: str) -> dict:
    """A run that produced nothing usable: the GET serves the deterministic set at once."""
    return {"status": "fallback", "reason": reason, "prompt_version": prompt_version}


def picks_from_result(result: dict[str, Any] | None) -> SuggestionAgentOutput | None:
    """The picks of a stored result, or None for a fallback marker, a miss or a corrupt entry.

    Never trusted as-is: ``assemble_set`` rebuilds every card from the LIVE registry, so a
    stored pick can only ever reorder cards the server would build anyway.
    """
    if not isinstance(result, dict) or result.get("status") != "ok":
        return None
    try:
        return SuggestionAgentOutput.model_validate({"ranked": result.get("picks")})
    except (ValueError, TypeError):
        return None


def get_result(thread_id: str, digest: str) -> dict[str, Any] | None:
    """The stored agent result for this digest, or None on a miss."""
    return _loads(_guard(lambda: _redis().get(_key(thread_id, digest))))


def put_result(thread_id: str, digest: str, value: dict[str, Any]) -> None:
    _guard(lambda: _redis().set(_key(thread_id, digest), json.dumps(value), ex=TTL_S))


def get_served(thread_id: str, digest: str) -> dict[str, Any] | None:
    return _loads(_guard(lambda: _redis().get(_key("served", thread_id, digest))))


def put_served(thread_id: str, digest: str, value: dict[str, Any]) -> dict[str, Any]:
    """Freeze ``value`` as the served set for this digest; returns whichever set won."""
    client = _redis()
    payload = json.dumps(value)
    if _guard(lambda: client.set(_key("served", thread_id, digest), payload, nx=True, ex=TTL_S)):
        return value
    return get_served(thread_id, digest) or value


def first_seen(thread_id: str, digest: str, now: float | None = None) -> float:
    """Epoch seconds a GET first saw this digest without a result (starts the wording wait)."""
    stamp = time.time() if now is None else now
    client = _redis()
    key = _key("seen", thread_id, digest)
    _guard(lambda: client.set(key, repr(stamp), nx=True, ex=TTL_S))
    raw = _guard(lambda: client.get(key))
    if isinstance(raw, bytes):
        raw = raw.decode()
    try:
        return float(raw)
    except (TypeError, ValueError):
        return stamp


def try_lock(thread_id: str, digest: str, ttl_s: int = LOCK_TTL_S, scope: str = "lock") -> bool:
    """Single-flight: True for exactly one caller per (scope, digest) per ``ttl_s``.

    ``lock`` dedupes the GET's lazy enqueues; ``inflight`` dedupes the task itself.
    """
    return bool(
        _guard(lambda: _redis().set(_key(scope, thread_id, digest), "1", nx=True, ex=ttl_s))
    )


def claim_refresh_slot(thread_id: str, window_s: int) -> bool:
    """True for exactly one caller per debounce window: the one that must enqueue the refresh.

    A refresh enqueued with a countdown of ``window_s`` reads the thread when it runs, so any
    trigger that lands inside the window is already covered by it and is dropped here. The slot
    expires with the window, so a trigger that lands after the run read the thread starts the next.
    """
    return try_lock(thread_id, "-", ttl_s=max(1, window_s), scope="debounce")


def release_lock(thread_id: str, digest: str, scope: str = "lock") -> None:
    try:
        _redis().delete(_key(scope, thread_id, digest))
    except Exception:  # noqa: BLE001 - best effort
        pass


def bump_runs(thread_id: str) -> int:
    """Increment and return the thread's agent run count."""
    client = _redis()
    key = _key("runs", thread_id)
    count = int(_guard(lambda: client.incr(key)))
    if count == 1:
        _guard(lambda: client.expire(key, RUNS_TTL_S))
    return count


def runs_so_far(thread_id: str) -> int:
    raw = _guard(lambda: _redis().get(_key("runs", thread_id)))
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def put_hint(thread_id: str, *, language: str, protocol: int | None) -> None:
    """Best effort: never raises (called on every GET)."""
    try:
        _redis().set(
            _key("hint", thread_id),
            json.dumps({"language": language, "protocol": protocol}),
            ex=HINT_TTL_S,
        )
    except Exception:  # noqa: BLE001
        pass


def get_hint(thread_id: str) -> dict[str, Any]:
    try:
        return _loads(_redis().get(_key("hint", thread_id))) or {}
    except Exception:  # noqa: BLE001
        return {}
