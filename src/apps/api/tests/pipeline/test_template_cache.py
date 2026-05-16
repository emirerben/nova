"""Tests for app.pipeline.template_cache.

The cache must:
  - Hash files via fingerprint (size + first/last 4MB), tolerate missing files.
  - Scope keys by prompt + schema version + analysis_mode.
  - Round-trip a real TemplateRecipe through serialize/deserialize.
  - Fall open on Redis errors (return None instead of raising).
  - Singleton its Redis client (one TCP handshake per worker).
"""

from unittest.mock import MagicMock

import pytest

from app.pipeline import template_cache
from app.pipeline.agents.gemini_analyzer import TemplateRecipe


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
    single_key = template_cache._cache_key("abc", "single")
    two_pass_key = template_cache._cache_key("abc", "two_pass")
    assert single_key != two_pass_key
    assert "single" in single_key
    assert "two_pass" in two_pass_key


def test_cache_key_includes_prompt_version():
    """Bumping TEMPLATE_PROMPT_VERSION must invalidate prior entries — guards
    against silently returning stale-prompt recipes after a prompt change."""
    key = template_cache._cache_key("abc", "single")
    assert template_cache.TEMPLATE_PROMPT_VERSION in key


def test_cache_key_includes_schema_version():
    """Bumping CACHE_SCHEMA_VERSION must invalidate prior entries — guards
    against silent TemplateRecipe field drift."""
    key = template_cache._cache_key("abc", "single")
    assert template_cache.CACHE_SCHEMA_VERSION in key


def test_cache_key_includes_fingerprint():
    key = template_cache._cache_key("some-hash-value", "single")
    assert "some-hash-value" in key


# ── round-trip ───────────────────────────────────────────────────────────────


def test_round_trip_get_after_set(fake_redis):
    recipe = _recipe()
    template_cache.set_cached_recipe("hash1", "single", recipe)
    got = template_cache.get_cached_recipe("hash1", "single")
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
    template_cache.set_cached_recipe("hash1", "single", recipe)
    got = template_cache.get_cached_recipe("hash1", "single")
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
    assert template_cache.get_cached_recipe("never-set", "single") is None


def test_analysis_mode_scoping_is_enforced_at_lookup(fake_redis):
    """A recipe cached under `single` must NOT be returned to a `two_pass` lookup."""
    template_cache.set_cached_recipe("hash1", "single", _recipe())
    assert template_cache.get_cached_recipe("hash1", "two_pass") is None
    # And the original is still findable under its own mode.
    assert template_cache.get_cached_recipe("hash1", "single") is not None


def test_ttl_is_set(fake_redis):
    """30-day TTL must be applied so the cache doesn't grow unbounded."""
    template_cache.set_cached_recipe("hash1", "single", _recipe())
    key = template_cache._cache_key("hash1", "single")
    assert fake_redis.ttls[key] == template_cache.CACHE_TTL_S
    assert template_cache.CACHE_TTL_S == 30 * 24 * 60 * 60


# ── fail-open behavior ───────────────────────────────────────────────────────


def test_get_falls_open_when_redis_unavailable(monkeypatch):
    monkeypatch.setattr(template_cache, "_get_redis", lambda: None)
    assert template_cache.get_cached_recipe("hash1", "single") is None


def test_set_falls_open_when_redis_unavailable(monkeypatch):
    monkeypatch.setattr(template_cache, "_get_redis", lambda: None)
    # Must not raise — fail-open is the contract.
    template_cache.set_cached_recipe("hash1", "single", _recipe())


def test_get_falls_open_on_redis_get_error(monkeypatch):
    fake = _FakeRedis()
    fake.get = MagicMock(side_effect=RuntimeError("network blip"))
    monkeypatch.setattr(template_cache, "_get_redis", lambda: fake)
    assert template_cache.get_cached_recipe("hash1", "single") is None


def test_get_falls_open_on_corrupt_cache_value(monkeypatch):
    """Garbage bytes in Redis must not crash the orchestrator."""
    fake = _FakeRedis()
    fake.store[template_cache._cache_key("hash1", "single")] = b"not-json-at-all"
    monkeypatch.setattr(template_cache, "_get_redis", lambda: fake)
    assert template_cache.get_cached_recipe("hash1", "single") is None


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
    fake.store[template_cache._cache_key("hash1", "single")] = payload.encode()
    monkeypatch.setattr(template_cache, "_get_redis", lambda: fake)
    assert template_cache.get_cached_recipe("hash1", "single") is None


def test_set_falls_open_on_redis_setex_error(monkeypatch):
    fake = _FakeRedis()
    fake.setex = MagicMock(side_effect=RuntimeError("network blip"))
    monkeypatch.setattr(template_cache, "_get_redis", lambda: fake)
    # Must not raise.
    template_cache.set_cached_recipe("hash1", "single", _recipe())


# ── degraded-recipe gate ─────────────────────────────────────────────────────


def test_set_skips_recipe_with_no_slots(fake_redis):
    """Empty slots = matcher has nothing to do = recipe is broken. Must not
    enter the cache or it pins a useless recipe for 30 days."""
    degraded = _recipe(slots=[], shot_count=5)
    template_cache.set_cached_recipe("hash1", "single", degraded)
    assert template_cache.get_cached_recipe("hash1", "single") is None


def test_set_skips_recipe_with_zero_shot_count(fake_redis):
    """shot_count == 0 is the structural signal that JSON recovery returned
    a partial recipe with pydantic defaults instead of raising."""
    degraded = _recipe(shot_count=0)
    template_cache.set_cached_recipe("hash1", "single", degraded)
    assert template_cache.get_cached_recipe("hash1", "single") is None


def test_set_accepts_well_formed_recipe(fake_redis):
    """The gate must NOT block legitimate recipes — the helper test_round_trip
    already covers this implicitly, but make the contract explicit."""
    healthy = _recipe(shot_count=7, slots=[{"position": 1, "slot_type": "hook"}])
    template_cache.set_cached_recipe("hash1", "single", healthy)
    assert template_cache.get_cached_recipe("hash1", "single") is not None


# ── connection-pooling guarantee ─────────────────────────────────────────────


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
