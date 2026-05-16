"""Redis-backed template-recipe cache.

Why this exists: a template reanalyze re-downloads the source video from GCS,
re-uploads it to the Gemini File API (the ACTIVE-poll alone is 30-60s on a
1080p template), and runs `analyze_template()` — the full Big-3-equivalent
LLM call. None of that work is content-dependent: same template bytes + same
prompt version + same analysis_mode always yield the same `TemplateRecipe`.
Fingerprint the source once, look up Redis, skip the Gemini round-trip on
hits.

Cache key shape:
    template_analysis:{prompt_version}:{schema_version}:{analysis_mode}:{fingerprint}

Bump:
  - PROMPT_VERSION when `analyze_template` / `TemplateRecipeAgent` prompts
    change in a way that affects recipe semantics (slot count, transitions,
    creative_direction, etc.)
  - SCHEMA_VERSION when `TemplateRecipe` gains/renames/drops a field. Without
    this, adding a new field would silently return cached entries with the
    default value for that field — a correctness bug because downstream code
    can't distinguish "cached from before the field existed" from "actually
    computed as default". Schema version is the cache's contract version.

`analysis_mode` is part of the key (not the prompt version) because the same
template legitimately has TWO valid recipes: one for single-pass, one for
two_pass. We don't want a mode flip to invalidate the entire cache, just to
scope lookups per mode.

Failure mode: Redis errors are non-fatal. The cache always falls open — a
cache failure produces the same behavior as a cache miss, never the wrong
recipe.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import threading
from typing import TYPE_CHECKING, Any

import redis as redis_lib
import structlog

from app.config import settings

if TYPE_CHECKING:
    from app.pipeline.agents.gemini_analyzer import TemplateRecipe

log = structlog.get_logger()

# Bump when `TemplateRecipeAgent` prompts (analyze_template_schema.txt,
# analyze_template_single.txt, analyze_template_pass2.txt) change in a way
# that affects recipe semantics. Old entries become invisible and eventually
# expire via TTL.
TEMPLATE_PROMPT_VERSION = "v1"

# Bump when `TemplateRecipe` gains/renames/drops a field.
CACHE_SCHEMA_VERSION = "s1"

# 30-day TTL. Template content is immutable per template_id+gcs_path; the
# cache shouldn't grow unbounded.
CACHE_TTL_S = 30 * 24 * 60 * 60

# Fast fingerprint instead of full-file sha256 — matches clip_cache.py's
# trade-off. Template videos are typically smaller than user clips, but the
# pattern is the same and avoids a multi-second pure-CPU hash per analyze.
_FINGERPRINT_HEAD_BYTES = 4 * 1024 * 1024
_FINGERPRINT_TAIL_BYTES = 4 * 1024 * 1024


def compute_template_hash(path: str) -> str | None:
    """File fingerprint = sha256(size || head 4MB || tail 4MB).

    Returns None if the file can't be read — callers treat None as "cannot
    cache, fall through to fresh analysis".
    """
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        log.warning("template_cache_stat_failed", path=path, error=str(exc))
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
        log.warning("template_cache_hash_failed", path=path, error=str(exc))
        return None
    return h.hexdigest()


def _cache_key(template_hash: str, analysis_mode: str) -> str:
    return (
        f"template_analysis:{TEMPLATE_PROMPT_VERSION}:{CACHE_SCHEMA_VERSION}"
        f":{analysis_mode}:{template_hash}"
    )


# Module-level pooled client. Same rationale as clip_cache — reusing the
# connection across the analyze hot path matters.
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
            log.warning("template_cache_redis_unavailable", error=str(exc))
            _redis_client = None
    return _redis_client


def get_cached_recipe(
    template_hash: str,
    analysis_mode: str,
) -> TemplateRecipe | None:
    """Return cached TemplateRecipe or None on miss / Redis failure / corrupt entry."""
    r = _get_redis()
    if r is None:
        return None
    key = _cache_key(template_hash, analysis_mode)
    try:
        raw = r.get(key)
    except Exception as exc:
        log.warning("template_cache_get_failed", key=key, error=str(exc))
        return None
    if raw is None:
        return None
    try:
        from app.pipeline.agents.gemini_analyzer import TemplateRecipe  # noqa: PLC0415

        data = json.loads(raw)
        return TemplateRecipe(**data)
    except Exception as exc:
        # Corrupt entry (e.g. schema drift on an entry written under SCHEMA_VERSION=s1
        # then read under s2 when forgot to bump). Logs loudly, falls open.
        log.warning("template_cache_decode_failed", key=key, error=str(exc))
        return None


def set_cached_recipe(
    template_hash: str,
    analysis_mode: str,
    recipe: TemplateRecipe,
) -> None:
    """Write TemplateRecipe to cache. Best-effort — failures are logged, not raised."""
    r = _get_redis()
    if r is None:
        return
    key = _cache_key(template_hash, analysis_mode)
    try:
        payload = dataclasses.asdict(recipe)
        r.setex(key, CACHE_TTL_S, json.dumps(payload))
    except Exception as exc:
        log.warning("template_cache_set_failed", key=key, error=str(exc))
