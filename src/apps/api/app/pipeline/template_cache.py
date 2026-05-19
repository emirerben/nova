"""Redis-backed template-recipe cache.

Why this exists: a template reanalyze re-downloads the source video from GCS,
re-uploads it to the Gemini File API (the ACTIVE-poll alone is 30-60s on a
1080p template), and runs `analyze_template()` — the full Big-3-equivalent
LLM call. None of that work is content-dependent: same template bytes + same
prompt version + same analysis_mode always yield the same `TemplateRecipe`.
Fingerprint the source once, look up Redis, skip the Gemini round-trip on
hits.

Cache key shape:
    template_analysis:{prompt_version}:{schema_version}:{agent_set}:{analysis_mode}:{fingerprint}

For agentic builds that run TemplateTextAgent, the key additionally encodes
`text_overlay_version` so Layer-1 (single Gemini call) and Layer-2 (multi-stage
OCR pipeline) results coexist as separate cache entries and never collide:

    template_analysis:{prompt_version}:{schema_version}:recipe+text:{analysis_mode}:{fingerprint}:v2

This is the "v1/v2 namespace". `text_overlay_version` is only appended for the
`recipe+text` agent_set; `recipe-only` (manual templates) is unaffected because
those builds never run the text agent.

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
#
# Kept at v1 even after TemplateTextAgent shipped (2026-05-17): the text
# agent runs only in the agentic build path, which uses a SEPARATE
# `agent_set` namespace ("recipe+text"). Manual templates ("recipe-only")
# don't see the text agent and don't need invalidation — bumping
# TEMPLATE_PROMPT_VERSION here would have forced a no-op re-analyze on
# every manual template the next time they touched cache.
TEMPLATE_PROMPT_VERSION = "v1"

# Bump when `TemplateRecipe` gains/renames/drops a field.
CACHE_SCHEMA_VERSION = "s1"

# Which set of agents produced the cached recipe. Manual templates use only
# TemplateRecipeAgent and key as "recipe-only"; agentic templates additionally
# run TemplateTextAgent and key as "recipe+text". Separating the namespaces
# means a future change to the text-extraction prompt only invalidates
# agentic caches, not manual ones.
AGENT_SET_RECIPE_ONLY = "recipe-only"
AGENT_SET_RECIPE_PLUS_TEXT = "recipe+text"

# Text-overlay pipeline version dimensions for agentic builds (recipe+text).
# "v1" = original single Gemini call via TemplateTextAgent.
# "v2" = multi-stage Layer-2 OCR pipeline (stages A–G) behind the
#         `text_overlay_v2_enabled` flag or `?use_layer2=true` override.
#
# These two versions produce different overlays for the same template video,
# so they must live in separate cache namespaces. Existing entries written
# before this PR have no version suffix and are treated as "v1" (the default)
# — they remain valid for Layer-1 builds with no migration needed.
#
# `recipe-only` builds (manual templates) never run the text agent and
# therefore have no v1/v2 distinction — `text_overlay_version` is ignored for
# that agent_set so their cache keys stay unchanged.
TEXT_OVERLAY_VERSION_V1 = "v1"
# IMPORTANT: bump this constant on EVERY Stage E (text_alignment) prompt
# change. The cache key embeds this value but does NOT embed the
# `text_alignment.prompt_version` field directly, so a prompt bump without
# a constant bump silently serves stale recipes.
#
# History:
#   2026-05-19 (v2 → v2-2026-05-19): v0.4.34.0 wired transcript_words into
#     agentic builds, rewrote Stage E to transcript-authoritative, fixed
#     Stage D dedup + Stage G newline normalize. Pre-fix recipes had garbage
#     like "if you if you put put in" (prod job 87b7292b).
#   2026-05-19 (v2-2026-05-19 → v2-2026-05-19-atomize): v0.4.34.2. v0.4.34.1
#     shipped the atomize_mode prompt branching but the cache constant wasn't
#     re-bumped, so reanalyze cache-hit on the v0.4.34.0 namespace and served
#     the multi-word-stuffed recipes from that run unchanged.
#
# When you change anything that affects Layer-2 overlay output (Stage E
# prompt, sanitizer logic, Stage D/G semantics), append a new suffix here.
TEXT_OVERLAY_VERSION_V2 = "v2-2026-05-19-atomize"

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


def _resolve_text_overlay_version(
    force_layer2: bool,
    settings_flag: bool,
) -> str:
    """Derive the text_overlay_version cache dimension from the per-request
    override and the global feature flag.

    Single source of truth so every call site uses identical routing logic.

    Args:
        force_layer2: per-request override from `?use_layer2=true` (PR #220).
        settings_flag: `settings.text_overlay_v2_enabled` global flag.

    Returns "v2" when either input signals Layer-2; "v1" otherwise.
    """
    return TEXT_OVERLAY_VERSION_V2 if (force_layer2 or settings_flag) else TEXT_OVERLAY_VERSION_V1


def _cache_key(
    template_hash: str,
    analysis_mode: str,
    agent_set: str = AGENT_SET_RECIPE_ONLY,
    text_overlay_version: str = TEXT_OVERLAY_VERSION_V1,
) -> str:
    """Build the Redis cache key.

    For ``agent_set="recipe+text"`` (agentic builds), ``text_overlay_version``
    is appended as an additional namespace dimension so Layer-1 and Layer-2
    results coexist:

        template_analysis:v1:s1:recipe+text:single:<hash>:v1   ← Layer-1
        template_analysis:v1:s1:recipe+text:single:<hash>:v2   ← Layer-2

    For ``agent_set="recipe-only"`` (manual templates) the version suffix is
    omitted — text_overlay_version is irrelevant for builds that never run the
    text agent, and appending it would force a cache miss on every existing
    manual-template entry.

    Backward-compat: existing v1 entries written before this PR have no
    version suffix, so callers requesting v1 would miss them. To preserve
    those entries we only append the suffix for v2 — v1 uses the pre-existing
    key shape (no suffix) so prior cache entries continue to hit.
    """
    base = (
        f"template_analysis:{TEMPLATE_PROMPT_VERSION}:{CACHE_SCHEMA_VERSION}"
        f":{agent_set}:{analysis_mode}:{template_hash}"
    )
    if agent_set == AGENT_SET_RECIPE_PLUS_TEXT and text_overlay_version != TEXT_OVERLAY_VERSION_V1:
        return f"{base}:{text_overlay_version}"
    return base


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
    agent_set: str = AGENT_SET_RECIPE_ONLY,
    text_overlay_version: str = TEXT_OVERLAY_VERSION_V1,
) -> TemplateRecipe | None:
    """Return cached TemplateRecipe or None on miss / Redis failure / corrupt entry.

    `agent_set` selects which build path's cache namespace to read from.
    Manual builds (analyze_template_task) use the default ("recipe-only");
    agentic builds (agentic_template_build_task) pass "recipe+text" so the
    two paths don't collide.

    `text_overlay_version` scopes agentic builds further by the text-overlay
    pipeline version ("v1" = Layer-1 single Gemini call, "v2" = Layer-2
    multi-stage OCR pipeline). Ignored for "recipe-only" agent_set. Use
    `_resolve_text_overlay_version()` to derive this from the per-request
    override and global flag rather than hardcoding it at call sites.
    """
    r = _get_redis()
    if r is None:
        return None
    key = _cache_key(template_hash, analysis_mode, agent_set, text_overlay_version)
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


def _is_degraded_recipe(recipe: TemplateRecipe) -> bool:
    """A recipe with no slots or zero shots is structurally unusable downstream
    (the matcher needs slots; the renderer needs shot_count). These show up
    when `analyze_template` recovers a partial JSON from Gemini and returns the
    pydantic defaults instead of raising. Refusing to cache them mirrors
    `clip_cache.set_cached_meta`'s `analysis_degraded`/`failed` gate — without
    this guard a single bad reanalyze would pin a broken recipe for 30 days."""
    return recipe.shot_count == 0 or not recipe.slots


def set_cached_recipe(
    template_hash: str,
    analysis_mode: str,
    recipe: TemplateRecipe,
    agent_set: str = AGENT_SET_RECIPE_ONLY,
    text_overlay_version: str = TEXT_OVERLAY_VERSION_V1,
) -> None:
    """Write TemplateRecipe to cache. Best-effort — failures are logged, not raised.

    `agent_set` selects which build path's cache namespace to write to.
    Mirrors `get_cached_recipe`'s parameter exactly so the two stay paired.

    `text_overlay_version` is forwarded to `_cache_key`; see `get_cached_recipe`
    for the full semantics. Both get and set must receive the same version so
    a future hit returns the entry written by this call.
    """
    if _is_degraded_recipe(recipe):
        log.warning(
            "template_cache_skip_degraded_recipe",
            template_hash=template_hash[:12],
            analysis_mode=analysis_mode,
            agent_set=agent_set,
            shot_count=recipe.shot_count,
            slot_count=len(recipe.slots),
        )
        return
    r = _get_redis()
    if r is None:
        return
    key = _cache_key(template_hash, analysis_mode, agent_set, text_overlay_version)
    try:
        payload = dataclasses.asdict(recipe)
        r.setex(key, CACHE_TTL_S, json.dumps(payload))
    except Exception as exc:
        log.warning("template_cache_set_failed", key=key, error=str(exc))
