"""Redis-backed clip_router assignment cache.

Why this exists: when an admin operator iterates in the Test tab — same clips,
same template — `clip_router` runs an LLM call per test job (~3-5s). The
assignments it produces are content-deterministic: same `ClipRouterInput` →
same logical assignments. Hash the input, look up Redis, skip the LLM call.

This is the smallest win in the Phase 1-4 series (Phases 1+2+3 closed reanalyze
latency; this one only saves the per-test-job hop) — the cache exists for the
"iterate test job with same clips" workflow, not for first-time analysis.

Cache key shape:
    clip_router:{prompt_version}:{schema_version}:{input_hash}

`prompt_version` is read from `ClipRouterAgent.spec.prompt_version` at lookup
time so cache invalidation follows the agent's declared version (per CLAUDE.md's
"Prompt-change rule"). Schema version is the cache's contract version for the
serialized assignments shape.

Input hashing is order-stable: candidates are sorted by id before hashing so
that "same clips, different upload order" yields the same key. The original
candidate ordering is preserved when calling the agent itself — the cache
canonicalizes only for the lookup.

Failure mode: Redis errors are non-fatal. The cache always falls open — a cache
failure produces the same behavior as a cache miss, never the wrong assignment.
"""

from __future__ import annotations

import hashlib
import json
import threading
from typing import TYPE_CHECKING, Any

import redis as redis_lib
import structlog

from app.config import settings

if TYPE_CHECKING:
    from app.agents.clip_router import ClipRouterInput, SlotAssignment

log = structlog.get_logger()

# Bump when `SlotAssignment` gains/renames/drops a field. Without this, adding
# e.g. `confidence: float` would silently return cached entries with default 0.0
# for the new field — same correctness hazard as template_cache.
ASSIGNMENT_SCHEMA_VERSION = "s1"

# 30-day TTL — matches template_cache / clip_cache.
CACHE_TTL_S = 30 * 24 * 60 * 60


def _get_prompt_version() -> str:
    """Read prompt_version from ClipRouterAgent.spec at call time.

    Defers the import to call time so module load doesn't pull in the agent
    runtime. Returns 'unknown' on any failure — that yields a key shape that
    can't collide with real production entries, so a cache lookup on the
    'unknown' key just returns None (fail-open)."""
    try:
        from app.agents.clip_router import ClipRouterAgent  # noqa: PLC0415

        return str(ClipRouterAgent.spec.prompt_version)
    except Exception as exc:
        log.warning("clip_router_cache_prompt_version_read_failed", error=str(exc))
        return "unknown"


def _canonical_input_hash(input_obj: ClipRouterInput) -> str:
    """sha256 of canonical JSON representation of the input.

    Candidates are sorted by id before serialization so that the same logical
    clip set yields the same hash regardless of upload order. Slots are NOT
    sorted (their `position` field is meaningful and ordering reflects template
    structure)."""
    dump = input_obj.model_dump(mode="json")
    candidates = dump.get("candidates", [])
    dump["candidates"] = sorted(candidates, key=lambda c: str(c.get("id", "")))
    return hashlib.sha256(
        json.dumps(dump, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _cache_key(input_hash: str) -> str:
    return f"clip_router:{_get_prompt_version()}:{ASSIGNMENT_SCHEMA_VERSION}:{input_hash}"


# Module-level pooled client. Same rationale as template_cache / clip_cache —
# avoiding fresh TCP handshakes on the hot path matters.
_redis_lock = threading.Lock()
_redis_client: redis_lib.Redis | None = None


def _get_redis() -> Any:
    """Return module-singleton Redis client. None on connection failure."""
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    with _redis_lock:
        if _redis_client is not None:
            return _redis_client
        try:
            _redis_client = redis_lib.from_url(
                settings.redis_url,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
            _redis_client.ping()
        except Exception as exc:
            log.warning("clip_router_cache_redis_unavailable", error=str(exc))
            _redis_client = None
    return _redis_client


def get_cached_assignments(
    input_obj: ClipRouterInput,
) -> list[SlotAssignment] | None:
    """Return cached assignments or None on miss / Redis failure / corrupt entry."""
    r = _get_redis()
    if r is None:
        return None
    key = _cache_key(_canonical_input_hash(input_obj))
    try:
        raw = r.get(key)
    except Exception as exc:
        log.warning("clip_router_cache_get_failed", key=key, error=str(exc))
        return None
    if raw is None:
        return None
    try:
        from app.agents.clip_router import SlotAssignment  # noqa: PLC0415

        data = json.loads(raw)
        return [SlotAssignment(**a) for a in data]
    except Exception as exc:
        # Corrupt entry or schema drift — fall open to fresh agent call.
        log.warning("clip_router_cache_decode_failed", key=key, error=str(exc))
        return None


def _is_degraded_assignments(assignments: list[SlotAssignment]) -> bool:
    """Empty assignments = the agent routed nothing, the matcher would fall
    through to greedy anyway. Refusing to cache them avoids pinning a
    no-op result for 30 days; the next run gets a fresh LLM call which may
    succeed."""
    return len(assignments) == 0


def set_cached_assignments(
    input_obj: ClipRouterInput,
    assignments: list[SlotAssignment],
) -> None:
    """Write assignments to cache. Best-effort — failures are logged, not raised."""
    if _is_degraded_assignments(assignments):
        log.warning(
            "clip_router_cache_skip_degraded_assignments",
            assignment_count=len(assignments),
        )
        return
    r = _get_redis()
    if r is None:
        return
    key = _cache_key(_canonical_input_hash(input_obj))
    try:
        payload = [a.model_dump(mode="json") for a in assignments]
        r.setex(key, CACHE_TTL_S, json.dumps(payload))
    except Exception as exc:
        log.warning("clip_router_cache_set_failed", key=key, error=str(exc))
