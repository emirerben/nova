"""Tests for app.pipeline.template_cache.

The cache must:
  - Hash files via fingerprint (size + first/last 4MB), tolerate missing files.
  - Scope keys by prompt + schema version + analysis_mode + template_id.
  - Scope agentic (recipe+text) keys by text_overlay_version (v1/v2).
  - Keep recipe-only (manual template) keys unchanged by text_overlay_version.
  - Round-trip a real TemplateRecipe through serialize/deserialize.
  - Fall open on Redis errors (return None instead of raising).
  - Singleton its Redis client (one TCP handshake per worker).
"""

from unittest.mock import MagicMock

import pytest

from app.pipeline import template_cache
from app.pipeline.agents.gemini_analyzer import TemplateRecipe
from app.pipeline.template_cache import (
    AGENT_SET_RECIPE_ONLY,
    AGENT_SET_RECIPE_PLUS_TEXT,
    TEXT_OVERLAY_VERSION_V1,
    TEXT_OVERLAY_VERSION_V2,
    _resolve_text_overlay_version,
)

# Used everywhere a template_id is required by the cache API. The exact value
# is irrelevant for most tests — it just needs to be consistent across paired
# get/set calls. Tests that specifically exercise the per-template scoping
# define their own ids inline.
_TID = "tmpl-test-001"


@pytest.fixture(autouse=True)
def _reset_redis_singleton():
    """Each test starts with no cached client; tests inject their own."""
    template_cache._redis_client = None
    yield
    template_cache._redis_client = None


class _FakeRedis:
    """Minimal subset of redis.Redis behavior used by template_cache."""

    def __init__(self):
        self.store: dict[str, bytes] = {}
        self.ttls: dict[str, int] = {}

    def ping(self):
        return True

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.store[key] = value.encode() if isinstance(value, str) else value
        self.ttls[key] = ttl


@pytest.fixture
def fake_redis(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(template_cache, "_get_redis", lambda: fake)
    return fake


def _recipe(**overrides):
    """Build a TemplateRecipe with the minimum required fields. Override per test."""
    base = {
        "shot_count": 7,
        "total_duration_s": 21.0,
        "hook_duration_s": 3.0,
        "slots": [{"position": 1, "target_duration_s": 3.0, "slot_type": "hook"}],
        "copy_tone": "punchy",
        "caption_style": "minimal",
        "beat_timestamps_s": [0.5, 1.0, 1.5],
        "creative_direction": "fast-cut travel montage",
    }
    base.update(overrides)
    return TemplateRecipe(**base)


# ── compute_template_hash ────────────────────────────────────────────────────


def test_compute_template_hash_returns_none_for_missing_file():
    assert template_cache.compute_template_hash("/nonexistent/template.mp4") is None


def test_compute_template_hash_distinct_content_distinct_hashes(tmp_path):
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    a.write_bytes(b"template-A" * 1024)
    b.write_bytes(b"different-B" * 1024)
    ha = template_cache.compute_template_hash(str(a))
    hb = template_cache.compute_template_hash(str(b))
    assert ha is not None
    assert hb is not None
    assert ha != hb


def test_compute_template_hash_same_content_same_hash(tmp_path):
    payload = b"identical template" * 4096
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    a.write_bytes(payload)
    b.write_bytes(payload)
    assert template_cache.compute_template_hash(str(a)) == template_cache.compute_template_hash(
        str(b)
    )


def test_compute_template_hash_includes_size_in_fingerprint(tmp_path):
    """Two files with the same head+tail bytes but different sizes must hash
    differently. Size is part of the fingerprint to guard against
    same-prefix-and-suffix aliasing (e.g. truncations)."""
    head = b"A" * (template_cache._FINGERPRINT_HEAD_BYTES + 1)
    middle_short = b"B" * 100
    middle_long = b"B" * 200
    tail = b"C" * (template_cache._FINGERPRINT_TAIL_BYTES + 1)
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    a.write_bytes(head + middle_short + tail)
    b.write_bytes(head + middle_long + tail)
    assert template_cache.compute_template_hash(str(a)) != template_cache.compute_template_hash(
        str(b)
    )


# ── cache key scoping ────────────────────────────────────────────────────────


def test_cache_key_scopes_by_analysis_mode():
    """A template can legitimately have TWO valid recipes: one per mode. The
    key must scope per mode so flipping mode doesn't return the wrong recipe."""
    single_key = template_cache._cache_key("abc", "single", template_id=_TID)
    two_pass_key = template_cache._cache_key("abc", "two_pass", template_id=_TID)
    assert single_key != two_pass_key
    assert "single" in single_key
    assert "two_pass" in two_pass_key


def test_cache_key_includes_prompt_version():
    """Bumping TEMPLATE_PROMPT_VERSION must invalidate prior entries — guards
    against silently returning stale-prompt recipes after a prompt change."""
    key = template_cache._cache_key("abc", "single", template_id=_TID)
    assert template_cache.TEMPLATE_PROMPT_VERSION in key


def test_cache_key_includes_schema_version():
    """Bumping CACHE_SCHEMA_VERSION must invalidate prior entries — guards
    against silent TemplateRecipe field drift."""
    key = template_cache._cache_key("abc", "single", template_id=_TID)
    assert template_cache.CACHE_SCHEMA_VERSION in key


def test_cache_key_schema_version_is_s2_or_later():
    """s1 keyed only by content hash. s2 added template_id. Bumping below s2
    would re-introduce the cross-template collision class."""
    assert template_cache.CACHE_SCHEMA_VERSION >= "s2", (
        "CACHE_SCHEMA_VERSION must be s2 or later — s1 colliding entries "
        "predate the template_id key segment and would serve wrong recipes."
    )


def test_cache_key_includes_fingerprint():
    key = template_cache._cache_key("some-hash-value", "single", template_id=_TID)
    assert "some-hash-value" in key


def test_cache_key_includes_template_id():
    """template_id must appear in the key so two templates that hash to the same
    fingerprint don't collide."""
    key = template_cache._cache_key("abc", "single", template_id="tmpl-XYZ")
    assert "tmpl-XYZ" in key


def test_cache_key_distinct_per_template_id():
    """Two templates with IDENTICAL template_hash but different template_id MUST
    produce different cache keys.

    Regression guard for the 2026-05-20 incident: "not just luck 2" was a fresh
    template that returned the cached recipe of "not just luck" because the
    cache was content-hash-keyed and the shared upload bytes hashed identically.
    """
    key_a = template_cache._cache_key("shared-hash", "single", template_id="template-A")
    key_b = template_cache._cache_key("shared-hash", "single", template_id="template-B")
    assert key_a != key_b, (
        "Two templates with the same source-video fingerprint must NOT share a "
        "cache key — that's exactly the bug this PR fixes."
    )


# ── round-trip ───────────────────────────────────────────────────────────────


def test_round_trip_get_after_set(fake_redis):
    recipe = _recipe()
    template_cache.set_cached_recipe("hash1", "single", recipe, template_id=_TID)
    got = template_cache.get_cached_recipe("hash1", "single", template_id=_TID)
    assert got is not None
    assert got.shot_count == 7
    assert got.creative_direction == "fast-cut travel montage"
    assert got.slots[0]["position"] == 1


def test_round_trip_preserves_all_fields(fake_redis):
    """Every field the renderer reads must round-trip through cache.

    This is the safety net for SCHEMA_VERSION discipline: when someone adds a
    new field to TemplateRecipe, this test reminds them to bump the schema
    version (otherwise old cached entries silently return the field's default).
    """
    recipe = _recipe(
        transition_style="whip-pan",
        color_grade="warm",
        pacing_style="energetic",
        sync_style="beat-snap",
        interstitials=[{"start_s": 5.0, "kind": "fade"}],
        subject_niche="travel",
        has_talking_head=True,
        has_voiceover=False,
        has_permanent_letterbox=False,
        min_slots=5,
        output_fit="letterbox",
        clip_filter_hint="ball in frame",
        font_default="Inter Tight",
    )
    template_cache.set_cached_recipe("hash1", "single", recipe, template_id=_TID)
    got = template_cache.get_cached_recipe("hash1", "single", template_id=_TID)
    assert got is not None
    assert got.transition_style == "whip-pan"
    assert got.color_grade == "warm"
    assert got.pacing_style == "energetic"
    assert got.sync_style == "beat-snap"
    assert got.interstitials == [{"start_s": 5.0, "kind": "fade"}]
    assert got.subject_niche == "travel"
    assert got.has_talking_head is True
    assert got.has_voiceover is False
    assert got.has_permanent_letterbox is False
    assert got.min_slots == 5
    assert got.output_fit == "letterbox"
    assert got.clip_filter_hint == "ball in frame"
    assert got.font_default == "Inter Tight"


def test_get_returns_none_on_miss(fake_redis):
    assert template_cache.get_cached_recipe("never-set", "single", template_id=_TID) is None


def test_analysis_mode_scoping_is_enforced_at_lookup(fake_redis):
    """A recipe cached under `single` must NOT be returned to a `two_pass` lookup."""
    template_cache.set_cached_recipe("hash1", "single", _recipe(), template_id=_TID)
    assert template_cache.get_cached_recipe("hash1", "two_pass", template_id=_TID) is None
    # And the original is still findable under its own mode.
    assert template_cache.get_cached_recipe("hash1", "single", template_id=_TID) is not None


def test_template_id_scoping_is_enforced_at_lookup(fake_redis):
    """A recipe cached under template_id "A" must NOT be returned to a lookup
    under template_id "B", even when template_hash matches.

    This is the per-key counterpart to test_cache_key_distinct_per_template_id —
    the previous test pins the key shape, this one pins the lookup behavior."""
    template_cache.set_cached_recipe("shared-hash", "single", _recipe(), template_id="A")
    assert template_cache.get_cached_recipe("shared-hash", "single", template_id="B") is None
    # And A's own lookup still finds it.
    assert template_cache.get_cached_recipe("shared-hash", "single", template_id="A") is not None


def test_ttl_is_set(fake_redis):
    """30-day TTL must be applied so the cache doesn't grow unbounded."""
    template_cache.set_cached_recipe("hash1", "single", _recipe(), template_id=_TID)
    key = template_cache._cache_key("hash1", "single", template_id=_TID)
    assert fake_redis.ttls[key] == template_cache.CACHE_TTL_S
    assert template_cache.CACHE_TTL_S == 30 * 24 * 60 * 60


# ── fail-open behavior ───────────────────────────────────────────────────────


def test_get_falls_open_when_redis_unavailable(monkeypatch):
    monkeypatch.setattr(template_cache, "_get_redis", lambda: None)
    assert template_cache.get_cached_recipe("hash1", "single", template_id=_TID) is None


def test_set_falls_open_when_redis_unavailable(monkeypatch):
    monkeypatch.setattr(template_cache, "_get_redis", lambda: None)
    # Must not raise — fail-open is the contract.
    template_cache.set_cached_recipe("hash1", "single", _recipe(), template_id=_TID)


def test_get_falls_open_on_redis_get_error(monkeypatch):
    fake = _FakeRedis()
    fake.get = MagicMock(side_effect=RuntimeError("network blip"))
    monkeypatch.setattr(template_cache, "_get_redis", lambda: fake)
    assert template_cache.get_cached_recipe("hash1", "single", template_id=_TID) is None


def test_get_falls_open_on_corrupt_cache_value(monkeypatch):
    """Garbage bytes in Redis must not crash the orchestrator."""
    fake = _FakeRedis()
    fake.store[template_cache._cache_key("hash1", "single", template_id=_TID)] = b"not-json-at-all"
    monkeypatch.setattr(template_cache, "_get_redis", lambda: fake)
    assert template_cache.get_cached_recipe("hash1", "single", template_id=_TID) is None


def test_get_falls_open_on_schema_drift(monkeypatch):
    """If an old entry deserializes into a field that no longer exists on
    TemplateRecipe (because someone forgot to bump SCHEMA_VERSION), the cache
    must fall open instead of crashing."""
    import json

    fake = _FakeRedis()
    # Pretend a future TemplateRecipe field exists in the cached entry.
    payload = json.dumps(
        {
            "shot_count": 7,
            "total_duration_s": 21.0,
            "hook_duration_s": 3.0,
            "slots": [],
            "copy_tone": "",
            "caption_style": "",
            "future_field_we_didnt_bump_schema_for": "boom",
        }
    )
    fake.store[template_cache._cache_key("hash1", "single", template_id=_TID)] = payload.encode()
    monkeypatch.setattr(template_cache, "_get_redis", lambda: fake)
    assert template_cache.get_cached_recipe("hash1", "single", template_id=_TID) is None


def test_set_falls_open_on_redis_setex_error(monkeypatch):
    fake = _FakeRedis()
    fake.setex = MagicMock(side_effect=RuntimeError("network blip"))
    monkeypatch.setattr(template_cache, "_get_redis", lambda: fake)
    # Must not raise.
    template_cache.set_cached_recipe("hash1", "single", _recipe(), template_id=_TID)


# ── degraded-recipe gate ─────────────────────────────────────────────────────


def test_set_skips_recipe_with_no_slots(fake_redis):
    """Empty slots = matcher has nothing to do = recipe is broken. Must not
    enter the cache or it pins a useless recipe for 30 days."""
    degraded = _recipe(slots=[], shot_count=5)
    template_cache.set_cached_recipe("hash1", "single", degraded, template_id=_TID)
    assert template_cache.get_cached_recipe("hash1", "single", template_id=_TID) is None


def test_set_skips_recipe_with_zero_shot_count(fake_redis):
    """shot_count == 0 is the structural signal that JSON recovery returned
    a partial recipe with pydantic defaults instead of raising."""
    degraded = _recipe(shot_count=0)
    template_cache.set_cached_recipe("hash1", "single", degraded, template_id=_TID)
    assert template_cache.get_cached_recipe("hash1", "single", template_id=_TID) is None


def test_set_accepts_well_formed_recipe(fake_redis):
    """The gate must NOT block legitimate recipes — the helper test_round_trip
    already covers this implicitly, but make the contract explicit."""
    healthy = _recipe(shot_count=7, slots=[{"position": 1, "slot_type": "hook"}])
    template_cache.set_cached_recipe("hash1", "single", healthy, template_id=_TID)
    assert template_cache.get_cached_recipe("hash1", "single", template_id=_TID) is not None


# ── call-site integration contract ───────────────────────────────────────────


def test_agentic_build_task_cache_check_precedes_gemini_upload():
    """Phase 3 load-bearing contract: in `agentic_template_build_task`, the
    cache lookup must run BEFORE `gemini_upload_and_wait`. If a future refactor
    moves the upload outside the `if recipe is None:` guard, cache hits would
    still pay the 30-60s upload cost — the cache becomes dead code.

    This is a source-inspection test rather than a full-task integration test
    because mocking the full agentic_template_build_task dependency chain
    (~15 boundaries) for one assertion is more brittle than this pin.
    """
    import inspect

    from app.tasks import agentic_template_build

    src = inspect.getsource(agentic_template_build.agentic_template_build_task)
    cache_call_idx = src.find("get_cached_recipe(")
    upload_call_idx = src.find("gemini_upload_and_wait(")
    assert cache_call_idx >= 0, "agentic build path must use get_cached_recipe"
    assert upload_call_idx >= 0, "agentic build path must still have a fallback upload"
    assert cache_call_idx < upload_call_idx, (
        "get_cached_recipe must be called BEFORE gemini_upload_and_wait — "
        "otherwise cache hits pay the upload cost and the cache is dead code"
    )


def test_manual_analyze_template_task_cache_check_precedes_gemini_upload():
    """Same contract for the manual reanalyze path."""
    import inspect

    from app.tasks import template_orchestrate

    src = inspect.getsource(template_orchestrate.analyze_template_task)
    cache_call_idx = src.find("get_cached_recipe(")
    upload_call_idx = src.find("gemini_upload_and_wait(")
    assert cache_call_idx >= 0, "manual analyze path must use get_cached_recipe"
    assert upload_call_idx >= 0, "manual analyze path must still have a fallback upload"
    assert cache_call_idx < upload_call_idx, (
        "get_cached_recipe must be called BEFORE gemini_upload_and_wait — "
        "otherwise cache hits pay the upload cost and the cache is dead code"
    )


def test_agentic_build_task_caches_after_analyze():
    """Phase 3 contract: the cache write must follow `analyze_template`, not
    precede it (you can't cache what hasn't been computed). Source-inspection
    pin for the same reason as the previous tests."""
    import inspect

    from app.tasks import agentic_template_build

    src = inspect.getsource(agentic_template_build.agentic_template_build_task)
    analyze_idx = src.find("analyze_template(")
    set_idx = src.find("set_cached_recipe(")
    assert analyze_idx >= 0
    assert set_idx >= 0
    assert analyze_idx < set_idx, (
        "set_cached_recipe must follow analyze_template — caching empty data is a bug"
    )


def test_agentic_build_task_passes_template_id_to_cache():
    """Both get and set calls in the agentic task must forward template_id —
    otherwise the call site raises TypeError at runtime (template_id is
    keyword-only and required)."""
    import inspect

    from app.tasks import agentic_template_build

    src = inspect.getsource(agentic_template_build.agentic_template_build_task)
    # Each cache call site must mention template_id= explicitly.
    assert src.count("template_id=template_id") >= 2, (
        "agentic_template_build_task must pass template_id= to BOTH "
        "get_cached_recipe and set_cached_recipe"
    )


def test_manual_analyze_task_passes_template_id_to_cache():
    """Same template_id-forwarding contract for the manual path."""
    import inspect

    from app.tasks import template_orchestrate

    src = inspect.getsource(template_orchestrate.analyze_template_task)
    assert src.count("template_id=template_id") >= 2, (
        "analyze_template_task must pass template_id= to BOTH get and set"
    )


def test_agentic_build_task_gates_cache_read_on_force():
    """force=True must skip the cache READ (still does the WRITE on success).

    Required so the admin reanalyze button actually reruns agents instead of
    cache-hitting on the prior recipe. Source-inspection pin so a future
    refactor that drops the `not force` guard fails this test loudly.
    """
    import inspect

    from app.tasks import agentic_template_build

    src = inspect.getsource(agentic_template_build.agentic_template_build_task)
    # The cache-read block must be guarded by `not force`.
    assert "not force" in src, (
        "agentic_template_build_task must gate get_cached_recipe on `not force` — "
        "without it, reanalyze cache-hits and no agents run"
    )


def test_manual_analyze_task_gates_cache_read_on_force():
    """Same force-bypass contract for the manual path."""
    import inspect

    from app.tasks import template_orchestrate

    src = inspect.getsource(template_orchestrate.analyze_template_task)
    assert "not force" in src, "analyze_template_task must gate get_cached_recipe on `not force`"


def test_admin_reanalyze_routes_pass_force_true():
    """Both reanalyze routes must enqueue with force=True. Without this, the
    cache short-circuits and the Re-run button silently does nothing."""
    import inspect

    from app.routes import admin

    src = inspect.getsource(admin.reanalyze_template_agentic)
    assert "force=True" in src, (
        "reanalyze_template_agentic must enqueue agentic_template_build_task "
        "with force=True so reanalyze actually reruns agents"
    )
    src_manual = inspect.getsource(admin.reanalyze_template)
    assert "force=True" in src_manual, (
        "reanalyze_template must enqueue analyze_template_task with force=True"
    )


# ── connection-pooling guarantee ─────────────────────────────────────────────


def test_get_redis_returns_singleton(monkeypatch):
    """We must NOT open a fresh Redis connection on every cache op — that
    erases most of the latency win on managed Redis. One construction per
    worker process."""
    construction_count = {"n": 0}

    class _CountingRedis(_FakeRedis):
        def __init__(self):
            construction_count["n"] += 1
            super().__init__()

    fake_redis_lib = MagicMock(from_url=MagicMock(return_value=_CountingRedis()))
    monkeypatch.setattr(template_cache, "redis_lib", fake_redis_lib)
    template_cache._redis_client = None  # reset singleton
    a = template_cache._get_redis()
    b = template_cache._get_redis()
    c = template_cache._get_redis()
    assert a is b is c
    assert construction_count["n"] == 1


# ── text_overlay_version namespace (v1/v2) ──────────────────────────────────


def test_v1_and_v2_keys_are_distinct():
    """Layer-1 and Layer-2 cache entries must never collide.

    Real-world evidence (2026-05-18 canary): ?use_layer2=true was silently
    discarded on cache-hit because the cache check ran before the override was
    consulted. Same template_hash + agent_set, different version → different key.
    """
    v1_key = template_cache._cache_key(
        "abc123",
        "single",
        AGENT_SET_RECIPE_PLUS_TEXT,
        TEXT_OVERLAY_VERSION_V1,
        template_id=_TID,
    )
    v2_key = template_cache._cache_key(
        "abc123",
        "single",
        AGENT_SET_RECIPE_PLUS_TEXT,
        TEXT_OVERLAY_VERSION_V2,
        template_id=_TID,
    )
    assert v1_key != v2_key


def test_v1_write_does_not_satisfy_v2_read(fake_redis):
    """Write v1, read v2 → cache miss. The namespaces are independent."""
    template_cache.set_cached_recipe(
        "hash1",
        "single",
        _recipe(),
        agent_set=AGENT_SET_RECIPE_PLUS_TEXT,
        text_overlay_version=TEXT_OVERLAY_VERSION_V1,
        template_id=_TID,
    )
    result = template_cache.get_cached_recipe(
        "hash1",
        "single",
        agent_set=AGENT_SET_RECIPE_PLUS_TEXT,
        text_overlay_version=TEXT_OVERLAY_VERSION_V2,
        template_id=_TID,
    )
    assert result is None, "v1 cache entry must not be returned for a v2 lookup"


def test_v2_write_does_not_satisfy_v1_read(fake_redis):
    """Write v2, read v1 → cache miss. The namespaces are independent."""
    template_cache.set_cached_recipe(
        "hash1",
        "single",
        _recipe(),
        agent_set=AGENT_SET_RECIPE_PLUS_TEXT,
        text_overlay_version=TEXT_OVERLAY_VERSION_V2,
        template_id=_TID,
    )
    result = template_cache.get_cached_recipe(
        "hash1",
        "single",
        agent_set=AGENT_SET_RECIPE_PLUS_TEXT,
        text_overlay_version=TEXT_OVERLAY_VERSION_V1,
        template_id=_TID,
    )
    assert result is None, "v2 cache entry must not be returned for a v1 lookup"


def test_v2_write_satisfies_v2_read(fake_redis):
    """Write v2, read v2 → cache hit with the correct recipe."""
    template_cache.set_cached_recipe(
        "hash1",
        "single",
        _recipe(shot_count=12),
        agent_set=AGENT_SET_RECIPE_PLUS_TEXT,
        text_overlay_version=TEXT_OVERLAY_VERSION_V2,
        template_id=_TID,
    )
    result = template_cache.get_cached_recipe(
        "hash1",
        "single",
        agent_set=AGENT_SET_RECIPE_PLUS_TEXT,
        text_overlay_version=TEXT_OVERLAY_VERSION_V2,
        template_id=_TID,
    )
    assert result is not None
    assert result.shot_count == 12


def test_v1_is_default_when_version_not_specified(fake_redis):
    """Legacy call signature (no text_overlay_version kwarg) must still work
    and use the v1 key — preserving backward-compat for existing cache entries
    written before this PR."""
    # Write using explicit v1.
    template_cache.set_cached_recipe(
        "hash1",
        "single",
        _recipe(shot_count=7),
        agent_set=AGENT_SET_RECIPE_PLUS_TEXT,
        text_overlay_version=TEXT_OVERLAY_VERSION_V1,
        template_id=_TID,
    )
    # Read without specifying version (legacy call site).
    result = template_cache.get_cached_recipe(
        "hash1",
        "single",
        agent_set=AGENT_SET_RECIPE_PLUS_TEXT,
        template_id=_TID,
    )
    assert result is not None
    assert result.shot_count == 7


def test_recipe_only_unaffected_by_version(fake_redis):
    """Manual templates (recipe-only agent_set) must not have their cache key
    changed by text_overlay_version — the text agent never runs for them.

    Concretely: write without version, read with explicit v2 → same key.
    """
    template_cache.set_cached_recipe("hash1", "single", _recipe(shot_count=3), template_id=_TID)
    # recipe-only has no v1/v2 dimension — v2 kwarg is ignored.
    result = template_cache.get_cached_recipe(
        "hash1",
        "single",
        agent_set=AGENT_SET_RECIPE_ONLY,
        text_overlay_version=TEXT_OVERLAY_VERSION_V2,
        template_id=_TID,
    )
    # Should still find the entry because recipe-only ignores the version dimension.
    assert result is not None
    assert result.shot_count == 3


def test_recipe_only_keys_are_identical_regardless_of_version():
    """The cache key for recipe-only must be the same regardless of which
    text_overlay_version is passed — ensures no silent key drift for manual
    templates after this PR."""
    key_default = template_cache._cache_key(
        "abc", "single", AGENT_SET_RECIPE_ONLY, template_id=_TID
    )
    key_v1 = template_cache._cache_key(
        "abc", "single", AGENT_SET_RECIPE_ONLY, TEXT_OVERLAY_VERSION_V1, template_id=_TID
    )
    key_v2 = template_cache._cache_key(
        "abc", "single", AGENT_SET_RECIPE_ONLY, TEXT_OVERLAY_VERSION_V2, template_id=_TID
    )
    assert key_default == key_v1 == key_v2, (
        "recipe-only cache key must be identical regardless of text_overlay_version — "
        f"got default={key_default!r}, v1={key_v1!r}, v2={key_v2!r}"
    )


# ── _resolve_text_overlay_version ───────────────────────────────────────────


def test_resolve_v2_when_force_layer2_true():
    assert (
        _resolve_text_overlay_version(force_layer2=True, settings_flag=False)
        == TEXT_OVERLAY_VERSION_V2
    )


def test_resolve_v2_when_settings_flag_true():
    assert (
        _resolve_text_overlay_version(force_layer2=False, settings_flag=True)
        == TEXT_OVERLAY_VERSION_V2
    )


def test_resolve_v2_when_both_true():
    assert (
        _resolve_text_overlay_version(force_layer2=True, settings_flag=True)
        == TEXT_OVERLAY_VERSION_V2
    )


def test_resolve_v1_when_both_false():
    assert _resolve_text_overlay_version(force_layer2=False, settings_flag=False) == "v1"


def test_text_overlay_version_v2_locked():
    """Lock the current namespace string so future devs don't bump it without
    intending to. The constant orphans cached recipes — every bump must be a
    conscious decision documented in the history block in template_cache.py.
    Bumped on 2026-05-22 with Stage E atomized-mode single-word defense:
    multi-word LLM outputs for atomized-input phrases now revert to OCR
    text so downstream `_is_atomized` stays true and `build_line_groups`
    can still group the phrase into a cumulative reveal (template 89cde014
    evidence — singleton overlays at 6.5s+ that broke the reveal pattern).
    """
    assert TEXT_OVERLAY_VERSION_V2 == "v2-2026-05-22-atomized-single-word"
