"""Redis-backed template-recipe cache.

Why this exists: a template reanalyze re-downloads the source video from GCS,
re-uploads it to the Gemini File API (the ACTIVE-poll alone is 30-60s on a
1080p template), and runs `analyze_template()` — the full Big-3-equivalent
LLM call. None of that work is content-dependent: same template bytes + same
prompt version + same analysis_mode always yield the same `TemplateRecipe`.
Fingerprint the source once, look up Redis, skip the Gemini round-trip on
hits.

Cache key shape:
    template_analysis:{prompt_version}:{schema_version}:{agent_set}:{analysis_mode}:{template_id}:{fingerprint}

`template_id` is part of the key so two distinct templates that share the same
source-video fingerprint do NOT collide. Prior to s2 the key was content-only;
a fresh template created from the same upload as an existing one would
cache-hit on the existing template's recipe and return it unchanged. This
caused the prod incident on 2026-05-20 ("not just luck 2 returns old recipe").
Schema bumped to s2 to orphan the colliding entries.

For agentic builds that run TemplateTextAgent, the key additionally encodes
`text_overlay_version` so Layer-1 (single Gemini call) and Layer-2 (multi-stage
OCR pipeline) results coexist as separate cache entries and never collide:

    template_analysis:{prompt_version}:{schema_version}:recipe+text:{analysis_mode}:{template_id}:{fingerprint}:v2

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
from pathlib import Path
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

# Bump when `TemplateRecipe` gains/renames/drops a field, OR when the cache
# key shape changes. Bumping orphans every existing entry — next access
# rebuilds from scratch and writes under the new key shape.
#
# History:
#   s1 → s2 (2026-05-20): added `template_id` to the cache key. Pre-s2 keys
#     were content-only, so two templates sharing a source-video fingerprint
#     collided on the same Redis entry (incident: "not just luck 2" returned
#     the old "not just luck" recipe on first analyze). s2 forces a one-time
#     rebuild wave but eliminates the collision class entirely.
CACHE_SCHEMA_VERSION = "s2"

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
# v1 stays a stable literal: the single Gemini call's behavior is already
# captured by the TEMPLATE_PROMPT_VERSION + CACHE_SCHEMA_VERSION namespace
# bumps. Keeping it as "v1" preserves all pre-existing v1 cache entries.
#
# v2 is now CONTENT-HASHED. Pre-2026-05-23 this was a manually-bumped string
# (`v2-2026-05-22-atomized-single-word`) that every Stage-E prompt edit had
# to be remembered to update; missing the bump silently served stale recipes.
# It is now derived deterministically from:
#   - the SHA-256 of every prompt file Layer-2 actually consumes
#   - the SHA-256 of every schema module Layer-2 actually consumes
#   - the `prompt_version` field on the Layer-2 AgentSpecs
#   - the JSON-encoded, sorted dict of relevant settings flags
# Any of those changing produces a fresh `v2-<short-hash>` string; existing
# entries become orphaned and recompute on next access.
#
# `recipe-only` builds (manual templates) never run the text agent and
# therefore have no v1/v2 distinction — `text_overlay_version` is ignored for
# that agent_set so their cache keys stay unchanged.
TEXT_OVERLAY_VERSION_V1 = "v1"

# Paths of the prompt files Layer-2 depends on. Order is irrelevant — the
# hashing helper sorts before concatenating — but keep this list narrow:
# every file in here invalidates every Layer-2 cache entry when its bytes
# change. Adding an unrelated prompt would cause spurious global invalidation.
_API_ROOT = Path(__file__).resolve().parents[2]  # .../src/apps/api
_LAYER2_PROMPT_PATHS: tuple[Path, ...] = (
    _API_ROOT / "prompts" / "extract_template_text.txt",
    _API_ROOT / "prompts" / "align_overlay_to_transcript.txt",
    _API_ROOT / "prompts" / "classify_overlay.txt",
    _API_ROOT / "prompts" / "transcribe.txt",
)

# Paths of the schema modules Layer-2 depends on. Same narrowness rule as the
# prompt list — fields these modules declare end up in the cached recipe, so
# any structural change must orphan prior entries.
_LAYER2_SCHEMA_PATHS: tuple[Path, ...] = (
    _API_ROOT / "app" / "agents" / "_schemas" / "template_text.py",
    _API_ROOT / "app" / "agents" / "_schemas" / "text_alignment.py",
    _API_ROOT / "app" / "agents" / "_schemas" / "text_classification.py",
    _API_ROOT / "app" / "agents" / "_schemas" / "text_overlay_ocr.py",
    _API_ROOT / "app" / "agents" / "_schemas" / "text_overlay_pipeline.py",
)


def _sha256_files(paths: tuple[Path, ...]) -> str:
    """SHA-256 over the bytes of every file in ``paths``, in sorted-by-path order.

    Stable across Python versions and OS file orderings. Missing files are
    represented by a sentinel so an accidental rename surfaces as a key change
    rather than a silent collision.
    """
    h = hashlib.sha256()
    for p in sorted(paths, key=lambda x: str(x)):
        h.update(str(p.name).encode("utf-8"))
        h.update(b"\x00")
        try:
            h.update(p.read_bytes())
        except OSError:
            # Path moved or got renamed — record the absence rather than
            # silently producing the same hash as before.
            h.update(b"__MISSING__")
        h.update(b"\x01")
    return h.hexdigest()


def compute_text_overlay_version(
    *,
    force_layer2: bool,
    settings_flag: bool,
    prompt_paths: tuple[Path, ...] = _LAYER2_PROMPT_PATHS,
    schema_paths: tuple[Path, ...] = _LAYER2_SCHEMA_PATHS,
    prompt_versions: tuple[str, ...] | None = None,
    settings_dict: dict[str, Any] | None = None,
) -> str:
    """Derive the text_overlay_version cache dimension from inputs that
    actually affect Layer-2 output.

    Returns the literal ``"v1"`` when neither input signals Layer-2 — the
    Layer-1 path is already content-namespaced by `TEMPLATE_PROMPT_VERSION` +
    `CACHE_SCHEMA_VERSION` and does not need this hash.

    For Layer-2, returns ``f"v2-{short_hash}"`` where ``short_hash`` is the
    first 16 hex chars of:

        sha256(
            sha256(prompt files) ||
            sha256(schema modules) ||
            sorted prompt_version strings ||
            sorted settings dict (JSON, sort_keys=True)
        )

    NOTE: Agent .py code edits that don't bump ``AgentSpec.prompt_version``
    will NOT invalidate the cache. This is a known gap; mitigated by a
    contributing-guide note and a future lint check (TODO — see
    TODOS.md "Cache invariants").
    """
    if not (force_layer2 or settings_flag):
        return TEXT_OVERLAY_VERSION_V1

    # Lazy import — avoids a top-level cycle (agents → cache → agents).
    if prompt_versions is None:
        from app.agents.template_text import TemplateTextAgent
        from app.agents.text_alignment import TextAlignmentAgent
        from app.agents.text_classification import TextClassificationAgent
        from app.agents.transcript import TranscriptAgent

        prompt_versions = (
            TemplateTextAgent.spec.prompt_version,
            TextAlignmentAgent.spec.prompt_version,
            TextClassificationAgent.spec.prompt_version,
            TranscriptAgent.spec.prompt_version,
        )

    if settings_dict is None:
        # Settings keys whose value affects Layer-2 routing/behavior. Keep
        # narrow — every key here invalidates every Layer-2 cache entry on
        # value change.
        settings_dict = {
            "text_overlay_v2_enabled": bool(getattr(settings, "text_overlay_v2_enabled", False)),
        }

    prompt_hash = _sha256_files(prompt_paths)
    schema_hash = _sha256_files(schema_paths)
    versions_blob = "|".join(sorted(prompt_versions)).encode("utf-8")
    settings_blob = json.dumps(settings_dict, sort_keys=True).encode("utf-8")

    h = hashlib.sha256()
    h.update(prompt_hash.encode("ascii"))
    h.update(b"\x00")
    h.update(schema_hash.encode("ascii"))
    h.update(b"\x00")
    h.update(versions_blob)
    h.update(b"\x00")
    h.update(settings_blob)
    short = h.hexdigest()[:16]
    return f"v2-{short}"


def __getattr__(name: str) -> str:
    """Module-level ``__getattr__`` so existing imports of
    ``TEXT_OVERLAY_VERSION_V2`` keep working — but resolve to the current
    content-hashed value rather than a frozen string constant.

    Lets call-site tests like ``test_agentic_build_cache_routing.py`` keep
    importing the symbol while the underlying value is derived from the
    Layer-2 prompt/schema content. Anything else is left to raise
    ``AttributeError`` the normal way.
    """
    if name == "TEXT_OVERLAY_VERSION_V2":
        return compute_text_overlay_version(
            force_layer2=True,
            settings_flag=False,
        )
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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

    Returns "v1" when neither input signals Layer-2; otherwise a
    content-hashed ``"v2-<short>"`` string derived from the Layer-2 prompt
    files, schema modules, agent `prompt_version` fields, and the relevant
    settings dict. See :func:`compute_text_overlay_version` for the hash
    contract.
    """
    return compute_text_overlay_version(
        force_layer2=force_layer2,
        settings_flag=settings_flag,
    )


def _cache_key(
    template_hash: str,
    analysis_mode: str,
    agent_set: str = AGENT_SET_RECIPE_ONLY,
    text_overlay_version: str = TEXT_OVERLAY_VERSION_V1,
    *,
    template_id: str,
) -> str:
    """Build the Redis cache key.

    ``template_id`` is part of the key so two templates that happen to share a
    source-video fingerprint do NOT collide. Required keyword-only — there is
    no sensible default; every call site must pass the row's identity.

    For ``agent_set="recipe+text"`` (agentic builds), ``text_overlay_version``
    is appended as an additional namespace dimension so Layer-1 and Layer-2
    results coexist:

        template_analysis:v1:s2:recipe+text:single:<id>:<hash>:v1   ← Layer-1
        template_analysis:v1:s2:recipe+text:single:<id>:<hash>:v2   ← Layer-2

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
        f":{agent_set}:{analysis_mode}:{template_id}:{template_hash}"
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
    *,
    template_id: str,
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

    `template_id` scopes the entry per-template so two templates sharing a
    source-video fingerprint don't collide. Required keyword-only.
    """
    r = _get_redis()
    if r is None:
        return None
    key = _cache_key(
        template_hash, analysis_mode, agent_set, text_overlay_version, template_id=template_id
    )
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


def invalidate_cache_for_template(template_id: str) -> int:
    """Delete every cached recipe entry tied to this `template_id`.

    Called from the admin overlay editor after a manual edit so a
    subsequent reanalyze doesn't cache-hit and silently overwrite the
    edit with the pre-edit recipe. Without this call, the path
    `admin edits recipe_cached in DB → admin clicks Reanalyze (no force) →
    agentic build cache-hits → returns pre-edit recipe → writes to DB →
    admin's edit gone` would happen on every normal reanalyze, not just
    forced ones.

    SCAN-based to handle the multi-key surface (one cache entry per
    {analysis_mode, agent_set, text_overlay_version} combination, all
    keyed by template_id). Returns the number of keys deleted; 0 on
    Redis failure or no-match. Best-effort — failures are logged, not
    raised, because losing a stale cache is a quality issue not a
    correctness one (the next build will just regenerate).
    """
    r = _get_redis()
    if r is None:
        return 0
    pattern = f"template_analysis:*:{template_id}:*"
    try:
        deleted = 0
        for key in r.scan_iter(match=pattern, count=200):
            r.delete(key)
            deleted += 1
        if deleted:
            log.info(
                "template_cache_invalidated",
                template_id=template_id,
                deleted=deleted,
            )
        return deleted
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "template_cache_invalidate_failed",
            template_id=template_id,
            error=str(exc),
        )
        return 0


def set_cached_recipe(
    template_hash: str,
    analysis_mode: str,
    recipe: TemplateRecipe,
    agent_set: str = AGENT_SET_RECIPE_ONLY,
    text_overlay_version: str = TEXT_OVERLAY_VERSION_V1,
    *,
    template_id: str,
) -> None:
    """Write TemplateRecipe to cache. Best-effort — failures are logged, not raised.

    `agent_set` selects which build path's cache namespace to write to.
    Mirrors `get_cached_recipe`'s parameter exactly so the two stay paired.

    `text_overlay_version` is forwarded to `_cache_key`; see `get_cached_recipe`
    for the full semantics. Both get and set must receive the same version so
    a future hit returns the entry written by this call.

    `template_id` must match the value passed to `get_cached_recipe` or a
    future read will miss this entry.
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
    key = _cache_key(
        template_hash, analysis_mode, agent_set, text_overlay_version, template_id=template_id
    )
    try:
        payload = dataclasses.asdict(recipe)
        r.setex(key, CACHE_TTL_S, json.dumps(payload))
    except Exception as exc:
        log.warning("template_cache_set_failed", key=key, error=str(exc))
