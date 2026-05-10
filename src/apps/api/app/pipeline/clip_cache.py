"""Redis-backed clip-analysis cache.

Why this exists: Gemini File-API upload + clip analysis is the slowest stretch
of the multi-clip template pipeline (~30-120s per clip on 8-clip jobs). The
result is content-addressable — the same clip bytes through the same prompt
always yield the same ClipMeta. Fingerprint the file once, look up Redis,
skip the Gemini round-trip on hits.

Cache key shape:
    clip_analysis:{prompt_version}:{schema_version}:{filter_hint_hash}:{fingerprint}

Rolling either version invalidates entries; rolling filter_hint scopes
naturally per-template. Bump:
  - PROMPT_VERSION when the analyze_clip prompt template changes (output
    semantics shift)
  - SCHEMA_VERSION when ClipMeta gains/renames/drops a field (deserialization
    of old entries would silently produce stale defaults)

Failure mode: Redis errors are non-fatal. The cache always falls open — a
cache failure produces the same behavior as a cache miss, never the wrong
ClipMeta.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

import redis as redis_lib
import structlog

from app.config import settings

if TYPE_CHECKING:
    from app.pipeline.agents.gemini_analyzer import ClipMeta

log = structlog.get_logger()

# Bump when analyze_clip's prompt template changes in a way that affects
# best_moments / hook_score / transcript output. Old entries become invisible
# and eventually expire via TTL.
CLIP_ANALYSIS_PROMPT_VERSION = "v1"

# Bump when ClipMeta gains/renames/drops a field. Without this, adding e.g.
# a new `confidence_score: float = 0.0` would silently return cached entries
# with the default value for the new field — a subtle correctness bug because
# downstream code can't distinguish "cached from before the field existed"
# from "actually scored 0.0". Schema version is the cache's contract version.
CACHE_SCHEMA_VERSION = "s1"

# 30-day TTL. Clip content is immutable but the cache shouldn't grow unbounded.
CACHE_TTL_S = 30 * 24 * 60 * 60

# Fast fingerprint instead of full-file sha256. Hashing a 1GB Android clip
# costs ~2-3s pure CPU on shared-vCPU; an 8-clip job would burn 16-24s of
# CPU contention with FFmpeg before any cache lookup happens. Two clips with
# matching size + first 4MB + last 4MB hash are extraordinarily unlikely to
# differ in content (would require a deliberate same-prefix-and-suffix
# collision). The trade-off is acceptable for our workload.
_FINGERPRINT_HEAD_BYTES = 4 * 1024 * 1024
_FINGERPRINT_TAIL_BYTES = 4 * 1024 * 1024


def compute_clip_hash(path: str) -> str | None:
    """File fingerprint = sha256(size || head 4MB || tail 4MB).

    Returns None if the file can't be read (missing, permission, etc.) —
    callers treat None as "cannot cache, fall through to fresh analysis".
    """
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        log.warning("clip_cache_stat_failed", path=path, error=str(exc))
        return None

    h = hashlib.sha256()
    h.update(str(size).encode("ascii"))
    try:
        with open(path, "rb") as f:
            head = f.read(_FINGERPRINT_HEAD_BYTES)
            h.update(head)
            if size > _FINGERPRINT_HEAD_BYTES + _FINGERPRINT_TAIL_BYTES:
                f.seek(-_FINGERPRINT_TAIL_BYTES, os.SEEK_END)
                tail = f.read(_FINGERPRINT_TAIL_BYTES)
                h.update(tail)
    except OSError as exc:
        log.warning("clip_cache_hash_failed", path=path, error=str(exc))
        return None
    return h.hexdigest()


def hash_clips_parallel(paths: list[str]) -> list[str | None]:
    """Fingerprint N clips concurrently. Per-clip failures yield None."""
    if not paths:
        return []
    # Cap workers at 4 — fingerprinting reads ~8MB per clip; over-parallelism
    # contends with concurrent FFmpeg processes for I/O bandwidth.
    with ThreadPoolExecutor(max_workers=min(len(paths), 4)) as pool:
        return list(pool.map(compute_clip_hash, paths))


def _filter_hint_key(filter_hint: str) -> str:
    """Stable 32-hex-char (128-bit) hash of filter_hint. Collision-safe well
    past any plausible cardinality of template filter hints."""
    if not filter_hint:
        return "none"
    return hashlib.sha256(filter_hint.encode("utf-8")).hexdigest()[:32]


def _cache_key(clip_hash: str, filter_hint: str) -> str:
    return (
        f"clip_analysis:{CLIP_ANALYSIS_PROMPT_VERSION}:{CACHE_SCHEMA_VERSION}"
        f":{_filter_hint_key(filter_hint)}:{clip_hash}"
    )


# Module-level pooled client. Reusing the connection (TCP + TLS handshake)
# matters: at 2 round-trips per cache op × N clips, a fresh connection per
# op would erase most of the latency win on managed Redis.
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
            # Smoke-test the connection so subsequent calls fast-path.
            _redis_client.ping()
        except Exception as exc:
            log.warning("clip_cache_redis_unavailable", error=str(exc))
            _redis_client = None
    return _redis_client


def get_cached_meta(clip_hash: str, filter_hint: str) -> ClipMeta | None:
    """Return cached ClipMeta or None on miss / Redis failure / corrupt entry."""
    r = _get_redis()
    if r is None:
        return None
    key = _cache_key(clip_hash, filter_hint)
    try:
        raw = r.get(key)
    except Exception as exc:
        log.warning("clip_cache_get_failed", key=key, error=str(exc))
        return None
    if raw is None:
        return None
    try:
        from app.pipeline.agents.gemini_analyzer import ClipMeta  # noqa: PLC0415
        data = json.loads(raw)
        # clip_path is call-site-specific; never serialized.
        data.pop("clip_path", None)
        return ClipMeta(**data)
    except Exception as exc:
        log.warning("clip_cache_decode_failed", key=key, error=str(exc))
        return None


def set_cached_meta(clip_hash: str, filter_hint: str, meta: ClipMeta) -> None:
    """Write ClipMeta to cache. Best-effort — failures are logged, not raised."""
    # Don't poison the cache with degraded fallbacks — those are job-specific
    # heuristics; a retry might succeed via the real Gemini path.
    if getattr(meta, "analysis_degraded", False) or getattr(meta, "failed", False):
        return
    r = _get_redis()
    if r is None:
        return
    key = _cache_key(clip_hash, filter_hint)
    try:
        payload = dataclasses.asdict(meta)
        payload.pop("clip_path", None)
        r.setex(key, CACHE_TTL_S, json.dumps(payload))
    except Exception as exc:
        log.warning("clip_cache_set_failed", key=key, error=str(exc))
