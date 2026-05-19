"""Tests for app.pipeline.clip_router_cache.

The cache must:
  - Canonicalize ClipRouterInput so same-clip-set, different-order yields same key.
  - Scope keys by prompt + schema version.
  - Round-trip SlotAssignment objects through serialize/deserialize.
  - Refuse to cache empty assignment lists (degraded).
  - Fall open on Redis errors (return None instead of raising).
  - Singleton its Redis client.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.agents.clip_router import (
    CandidateClip,
    ClipRouterInput,
    SlotAssignment,
    SlotSpec,
)
from app.pipeline import clip_router_cache


@pytest.fixture(autouse=True)
def _reset_redis_singleton():
    clip_router_cache._redis_client = None
    yield
    clip_router_cache._redis_client = None


class _FakeRedis:
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
    monkeypatch.setattr(clip_router_cache, "_get_redis", lambda: fake)
    return fake


def _slot(position: int = 1, slot_type: str = "broll", duration: float = 3.0) -> SlotSpec:
    return SlotSpec(position=position, slot_type=slot_type, target_duration_s=duration)


def _candidate(id: str = "files/abc", duration: float = 3.0, energy: float = 7.0) -> CandidateClip:  # noqa: A002
    return CandidateClip(id=id, duration_s=duration, energy=energy, hook_score=6.0, description="x")


def _input(**overrides) -> ClipRouterInput:
    base = {
        "slots": [_slot(position=1, slot_type="hook"), _slot(position=2, slot_type="broll")],
        "candidates": [_candidate(id="files/a"), _candidate(id="files/b")],
        "copy_tone": "punchy",
        "creative_direction": "fast-cut travel",
    }
    base.update(overrides)
    return ClipRouterInput(**base)


def _assignment(slot_position: int = 1, candidate_id: str = "files/a") -> SlotAssignment:
    return SlotAssignment(slot_position=slot_position, candidate_id=candidate_id, rationale="")


# ── canonical input hashing ─────────────────────────────────────────────────


def test_same_input_same_hash():
    a = clip_router_cache._canonical_input_hash(_input())
    b = clip_router_cache._canonical_input_hash(_input())
    assert a == b


def test_candidate_order_does_not_affect_hash():
    """Same logical clip set in different upload orders must hash identically —
    the cache canonicalizes candidate order so test-job reruns hit even if
    clips were re-uploaded in a different order."""
    a = clip_router_cache._canonical_input_hash(
        _input(candidates=[_candidate(id="files/a"), _candidate(id="files/b")])
    )
    b = clip_router_cache._canonical_input_hash(
        _input(candidates=[_candidate(id="files/b"), _candidate(id="files/a")])
    )
    assert a == b


def test_different_candidate_content_distinct_hash():
    a = clip_router_cache._canonical_input_hash(
        _input(candidates=[_candidate(id="files/a", energy=7.0), _candidate(id="files/b")])
    )
    b = clip_router_cache._canonical_input_hash(
        _input(candidates=[_candidate(id="files/a", energy=2.0), _candidate(id="files/b")])
    )
    assert a != b


def test_slot_change_changes_hash():
    """Slots define template structure — a recipe edit must invalidate the
    cache. (Slot position/type/duration changes are user-visible edits.)"""
    a = clip_router_cache._canonical_input_hash(_input())
    b = clip_router_cache._canonical_input_hash(
        _input(
            slots=[_slot(position=1, slot_type="hook"), _slot(position=2, slot_type="punchline")]
        )
    )
    assert a != b


def test_copy_tone_change_changes_hash():
    a = clip_router_cache._canonical_input_hash(_input(copy_tone="punchy"))
    b = clip_router_cache._canonical_input_hash(_input(copy_tone="calm"))
    assert a != b


def test_creative_direction_change_changes_hash():
    a = clip_router_cache._canonical_input_hash(_input(creative_direction="fast"))
    b = clip_router_cache._canonical_input_hash(_input(creative_direction="slow"))
    assert a != b


# ── cache key scoping ───────────────────────────────────────────────────────


def test_cache_key_includes_prompt_version():
    """The key embeds the agent's prompt_version. When prompt_version on
    ClipRouterAgent.spec changes, all old entries become invisible."""
    key = clip_router_cache._cache_key("abc")
    # Default reads ClipRouterAgent.spec.prompt_version which is non-empty.
    assert "clip_router:" in key
    assert "abc" in key


def test_cache_key_includes_schema_version():
    key = clip_router_cache._cache_key("abc")
    assert clip_router_cache.ASSIGNMENT_SCHEMA_VERSION in key


def test_prompt_version_falls_back_to_unknown_on_import_failure():
    """If ClipRouterAgent can't be imported (test isolation, broken deps), the
    cache must NOT crash — returns 'unknown' so the key is well-formed but
    can't collide with real entries."""
    with patch.dict("sys.modules", {"app.agents.clip_router": None}):
        version = clip_router_cache._get_prompt_version()
    assert version == "unknown"


# ── round-trip ──────────────────────────────────────────────────────────────


def test_round_trip_get_after_set(fake_redis):
    inp = _input()
    assignments = [
        _assignment(slot_position=1, candidate_id="files/a"),
        _assignment(slot_position=2, candidate_id="files/b"),
    ]
    clip_router_cache.set_cached_assignments(inp, assignments)
    got = clip_router_cache.get_cached_assignments(inp)
    assert got is not None
    assert len(got) == 2
    assert got[0].slot_position == 1
    assert got[0].candidate_id == "files/a"
    assert got[1].slot_position == 2
    assert got[1].candidate_id == "files/b"


def test_round_trip_preserves_rationale(fake_redis):
    """Rationale is part of the SlotAssignment schema — schema-version discipline."""
    inp = _input()
    assignments = [
        SlotAssignment(slot_position=1, candidate_id="files/a", rationale="best hook energy"),
    ]
    clip_router_cache.set_cached_assignments(inp, assignments)
    got = clip_router_cache.get_cached_assignments(inp)
    assert got is not None
    assert got[0].rationale == "best hook energy"


def test_get_returns_none_on_miss(fake_redis):
    assert clip_router_cache.get_cached_assignments(_input()) is None


def test_reorder_candidates_still_hits_cache(fake_redis):
    """End-to-end equivalent of test_candidate_order_does_not_affect_hash —
    proves the canonicalization actually flows through to lookup."""
    inp_a = _input(candidates=[_candidate(id="files/a"), _candidate(id="files/b")])
    inp_b = _input(candidates=[_candidate(id="files/b"), _candidate(id="files/a")])
    clip_router_cache.set_cached_assignments(inp_a, [_assignment()])
    got = clip_router_cache.get_cached_assignments(inp_b)
    assert got is not None


def test_ttl_is_set(fake_redis):
    clip_router_cache.set_cached_assignments(_input(), [_assignment()])
    key = clip_router_cache._cache_key(clip_router_cache._canonical_input_hash(_input()))
    assert fake_redis.ttls[key] == clip_router_cache.CACHE_TTL_S
    assert clip_router_cache.CACHE_TTL_S == 30 * 24 * 60 * 60


# ── degraded-assignment gate ────────────────────────────────────────────────


def test_set_skips_empty_assignments(fake_redis):
    """Empty assignments = LLM routed nothing = greedy fallback would have run
    anyway. Don't pin a no-op result for 30 days."""
    clip_router_cache.set_cached_assignments(_input(), [])
    assert clip_router_cache.get_cached_assignments(_input()) is None


def test_set_accepts_non_empty_assignments(fake_redis):
    clip_router_cache.set_cached_assignments(_input(), [_assignment()])
    assert clip_router_cache.get_cached_assignments(_input()) is not None


# ── fail-open behavior ──────────────────────────────────────────────────────


def test_get_falls_open_when_redis_unavailable(monkeypatch):
    monkeypatch.setattr(clip_router_cache, "_get_redis", lambda: None)
    assert clip_router_cache.get_cached_assignments(_input()) is None


def test_set_falls_open_when_redis_unavailable(monkeypatch):
    monkeypatch.setattr(clip_router_cache, "_get_redis", lambda: None)
    # Must not raise.
    clip_router_cache.set_cached_assignments(_input(), [_assignment()])


def test_get_falls_open_on_redis_get_error(monkeypatch):
    fake = _FakeRedis()
    fake.get = MagicMock(side_effect=RuntimeError("network blip"))
    monkeypatch.setattr(clip_router_cache, "_get_redis", lambda: fake)
    assert clip_router_cache.get_cached_assignments(_input()) is None


def test_get_falls_open_on_corrupt_cache_value(monkeypatch):
    fake = _FakeRedis()
    key = clip_router_cache._cache_key(clip_router_cache._canonical_input_hash(_input()))
    fake.store[key] = b"not-json-at-all"
    monkeypatch.setattr(clip_router_cache, "_get_redis", lambda: fake)
    assert clip_router_cache.get_cached_assignments(_input()) is None


def test_get_falls_open_on_schema_drift(monkeypatch):
    """A cached entry that decodes into an unknown SlotAssignment field shape
    must not crash — fall open to fresh agent call."""
    import json

    fake = _FakeRedis()
    # Future-shape payload: extra field that SlotAssignment doesn't know.
    payload = json.dumps(
        [{"slot_position": 1, "candidate_id": "files/a", "rationale": "", "future_field": "boom"}]
    )
    key = clip_router_cache._cache_key(clip_router_cache._canonical_input_hash(_input()))
    fake.store[key] = payload.encode()
    monkeypatch.setattr(clip_router_cache, "_get_redis", lambda: fake)
    # Pydantic models by default reject extra fields; if they don't, this test
    # still passes (no exception). Either way: behavior is fail-open or hit.
    result = clip_router_cache.get_cached_assignments(_input())
    # Acceptable: None (rejected) or a list (accepted with extra ignored).
    # The contract is "must not raise" — assert either is True.
    assert result is None or isinstance(result, list)


def test_set_falls_open_on_redis_setex_error(monkeypatch):
    fake = _FakeRedis()
    fake.setex = MagicMock(side_effect=RuntimeError("network blip"))
    monkeypatch.setattr(clip_router_cache, "_get_redis", lambda: fake)
    # Must not raise.
    clip_router_cache.set_cached_assignments(_input(), [_assignment()])


# ── call-site integration contract ──────────────────────────────────────────


def test_agentic_match_cache_check_precedes_agent_run():
    """Phase 4 load-bearing contract: `agentic_match()` must call the cache
    BEFORE invoking `agent.run()`. If a future refactor moves the agent call
    outside the `if assignments is None:` guard, the cache becomes dead code.

    Source-inspection test (same rationale as the Phase 3 contract tests in
    test_template_cache.py — mocking the full agentic_match dependency chain
    for one structural assertion is more brittle than this pin)."""
    import inspect

    from app.pipeline import agentic_matcher

    src = inspect.getsource(agentic_matcher.agentic_match)
    cache_call_idx = src.find("get_cached_assignments(")
    agent_run_idx = src.find("agent.run(")
    assert cache_call_idx >= 0, "agentic_match must use get_cached_assignments"
    assert agent_run_idx >= 0, "agentic_match must still call agent.run() on miss"
    assert cache_call_idx < agent_run_idx, (
        "get_cached_assignments must precede agent.run — otherwise hits pay the LLM cost"
    )


def test_agentic_match_caches_after_agent_run():
    """The cache write must follow `agent.run` (can't cache what isn't computed)."""
    import inspect

    from app.pipeline import agentic_matcher

    src = inspect.getsource(agentic_matcher.agentic_match)
    run_idx = src.find("agent.run(")
    set_idx = src.find("set_cached_assignments(")
    assert run_idx >= 0
    assert set_idx >= 0
    assert run_idx < set_idx, (
        "set_cached_assignments must follow agent.run — caching no-op data is a bug"
    )


# ── connection-pooling guarantee ────────────────────────────────────────────


def test_get_redis_returns_singleton(monkeypatch):
    construction_count = {"n": 0}

    class _CountingRedis(_FakeRedis):
        def __init__(self):
            construction_count["n"] += 1
            super().__init__()

    fake_redis_lib = MagicMock(from_url=MagicMock(return_value=_CountingRedis()))
    monkeypatch.setattr(clip_router_cache, "redis_lib", fake_redis_lib)
    clip_router_cache._redis_client = None
    a = clip_router_cache._get_redis()
    b = clip_router_cache._get_redis()
    c = clip_router_cache._get_redis()
    assert a is b is c
    assert construction_count["n"] == 1
