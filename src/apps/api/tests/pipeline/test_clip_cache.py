"""Tests for app.pipeline.clip_cache.

The cache must:
  - Hash files via fingerprint (size + first/last 4MB), tolerate missing files.
  - Scope keys by prompt + schema version + filter_hint.
  - Refuse to cache degraded fallback metas.
  - Fall open on Redis errors (return None instead of raising).
  - Round-trip a real ClipMeta through serialize/deserialize.
"""

from unittest.mock import MagicMock

import pytest

from app.pipeline import clip_cache
from app.pipeline.agents.gemini_analyzer import ClipMeta


@pytest.fixture(autouse=True)
def _reset_redis_singleton():
    """Each test starts with no cached client; tests inject their own."""
    clip_cache._redis_client = None
    yield
    clip_cache._redis_client = None


class _FakeRedis:
    """Minimal subset of redis.Redis behavior used by clip_cache."""

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
    monkeypatch.setattr(clip_cache, "_get_redis", lambda: fake)
    return fake


def _meta(**overrides):
    base = {
        "clip_id": "files/abc",
        "transcript": "hello",
        "hook_text": "Hook!",
        "hook_score": 8.5,
        "best_moments": [{"start_s": 0.0, "end_s": 3.0, "energy": 9.0, "description": "x"}],
        "detected_subject": "demo",
    }
    base.update(overrides)
    return ClipMeta(**base)


# ── compute_clip_hash + hash_clips_parallel ──────────────────────────────────


def test_compute_clip_hash_returns_none_for_missing_file():
    assert clip_cache.compute_clip_hash("/nonexistent/clip.mp4") is None


def test_compute_clip_hash_distinct_content_distinct_hashes(tmp_path):
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    a.write_bytes(b"hello-clip-A" * 1024)
    b.write_bytes(b"different-bytes-B" * 1024)
    ha = clip_cache.compute_clip_hash(str(a))
    hb = clip_cache.compute_clip_hash(str(b))
    assert ha is not None
    assert hb is not None
    assert ha != hb


def test_compute_clip_hash_same_content_same_hash(tmp_path):
    payload = b"identical content" * 4096
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    a.write_bytes(payload)
    b.write_bytes(payload)
    assert clip_cache.compute_clip_hash(str(a)) == clip_cache.compute_clip_hash(str(b))


def test_compute_clip_hash_includes_size_in_fingerprint(tmp_path):
    """Two files with the same head+tail bytes but different sizes must hash differently.

    Head and tail are byte-identical; only size differs. Size is part of the
    fingerprint so they hash differently — this guards against the attack/edge
    case where same-prefix-and-suffix files (e.g. truncations) would alias."""
    head = b"A" * (clip_cache._FINGERPRINT_HEAD_BYTES + 1)
    middle_short = b"B" * 100
    middle_long = b"B" * 200
    tail = b"C" * (clip_cache._FINGERPRINT_TAIL_BYTES + 1)
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    a.write_bytes(head + middle_short + tail)
    b.write_bytes(head + middle_long + tail)
    assert clip_cache.compute_clip_hash(str(a)) != clip_cache.compute_clip_hash(str(b))


def test_hash_clips_parallel_handles_mix_of_present_and_missing(tmp_path):
    a = tmp_path / "a.mp4"
    a.write_bytes(b"x" * 1024)
    out = clip_cache.hash_clips_parallel([str(a), "/nope.mp4"])
    assert out[0] is not None
    assert out[1] is None


def test_hash_clips_parallel_empty_input():
    assert clip_cache.hash_clips_parallel([]) == []


# ── cache key scoping ────────────────────────────────────────────────────────


def test_cache_key_scopes_by_filter_hint():
    a = clip_cache._cache_key("abc", "ball")
    b = clip_cache._cache_key("abc", "no-ball")
    assert a != b


def test_cache_key_includes_prompt_version():
    """If we ever bump CLIP_ANALYSIS_PROMPT_VERSION, all old entries become
    invisible (different prefix), preventing stale-prompt cache hits."""
    key = clip_cache._cache_key("abc", "ball")
    assert clip_cache.CLIP_ANALYSIS_PROMPT_VERSION in key


def test_cache_key_includes_schema_version():
    """Bumping CACHE_SCHEMA_VERSION must invalidate prior entries — guards
    against silent ClipMeta field drift."""
    key = clip_cache._cache_key("abc", "ball")
    assert clip_cache.CACHE_SCHEMA_VERSION in key


def test_filter_hint_key_uses_128_bits():
    """Truncated to 32 hex chars (128 bits) — collision-safe for any plausible
    filter_hint cardinality."""
    assert len(clip_cache._filter_hint_key("any hint")) == 32


def test_filter_hint_key_empty_returns_sentinel():
    assert clip_cache._filter_hint_key("") == "none"


# ── round-trip ───────────────────────────────────────────────────────────────


def test_round_trip_get_after_set(fake_redis):
    clip_cache.set_cached_meta("hash1", "ball", _meta())
    got = clip_cache.get_cached_meta("hash1", "ball")
    assert got is not None
    assert got.clip_id == "files/abc"
    assert got.hook_score == 8.5


def test_get_returns_none_on_miss(fake_redis):
    assert clip_cache.get_cached_meta("never-set", "ball") is None


def test_filter_hint_scoping_is_enforced_at_lookup(fake_redis):
    clip_cache.set_cached_meta("hash1", "ball", _meta())
    assert clip_cache.get_cached_meta("hash1", "different") is None


def test_set_skips_degraded_metas(fake_redis):
    deg = _meta(analysis_degraded=True)
    clip_cache.set_cached_meta("hash1", "ball", deg)
    assert clip_cache.get_cached_meta("hash1", "ball") is None


def test_set_skips_failed_metas(fake_redis):
    failed = _meta(failed=True)
    clip_cache.set_cached_meta("hash1", "ball", failed)
    assert clip_cache.get_cached_meta("hash1", "ball") is None


def test_clip_path_never_serialized(fake_redis):
    """clip_path is call-site-specific (the local download path on this run);
    it must not survive into the cache."""
    meta = _meta()
    meta.clip_path = "/tmp/run-specific/clip.mp4"
    clip_cache.set_cached_meta("hash1", "ball", meta)
    got = clip_cache.get_cached_meta("hash1", "ball")
    assert got is not None
    # The reconstructed meta has the dataclass default ("")
    assert got.clip_path == ""


# ── fail-open behavior ───────────────────────────────────────────────────────


def test_get_falls_open_when_redis_unavailable(monkeypatch):
    monkeypatch.setattr(clip_cache, "_get_redis", lambda: None)
    assert clip_cache.get_cached_meta("hash1", "ball") is None


def test_set_falls_open_when_redis_unavailable(monkeypatch):
    monkeypatch.setattr(clip_cache, "_get_redis", lambda: None)
    # Must not raise
    clip_cache.set_cached_meta("hash1", "ball", _meta())


def test_get_falls_open_on_redis_get_error(monkeypatch):
    fake = _FakeRedis()
    fake.get = MagicMock(side_effect=RuntimeError("network blip"))
    monkeypatch.setattr(clip_cache, "_get_redis", lambda: fake)
    assert clip_cache.get_cached_meta("hash1", "ball") is None


def test_get_falls_open_on_corrupt_cache_value(monkeypatch):
    """A garbage value in Redis must not crash the orchestrator."""
    fake = _FakeRedis()
    fake.store[clip_cache._cache_key("hash1", "ball")] = b"not-json-at-all"
    monkeypatch.setattr(clip_cache, "_get_redis", lambda: fake)
    assert clip_cache.get_cached_meta("hash1", "ball") is None


def test_set_falls_open_on_redis_setex_error(monkeypatch):
    fake = _FakeRedis()
    fake.setex = MagicMock(side_effect=RuntimeError("network blip"))
    monkeypatch.setattr(clip_cache, "_get_redis", lambda: fake)
    # Must not raise
    clip_cache.set_cached_meta("hash1", "ball", _meta())


# ── connection-pooling guarantee ─────────────────────────────────────────────


def test_get_redis_returns_singleton(monkeypatch):
    """We must NOT open a fresh Redis connection on every cache op — that
    erases most of the latency win on managed Redis."""
    construction_count = {"n": 0}

    class _CountingRedis(_FakeRedis):
        def __init__(self):
            construction_count["n"] += 1
            super().__init__()

    fake_redis_lib = MagicMock(from_url=MagicMock(return_value=_CountingRedis()))
    monkeypatch.setattr(clip_cache, "redis_lib", fake_redis_lib)
    clip_cache._redis_client = None  # reset singleton
    a = clip_cache._get_redis()
    b = clip_cache._get_redis()
    c = clip_cache._get_redis()
    assert a is b is c
    assert construction_count["n"] == 1
