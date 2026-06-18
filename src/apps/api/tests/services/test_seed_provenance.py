"""Unit tests for seed_provenance.match_specs_to_seeds.

Locks:
- Overlap match assigns the best-scoring (spec, seed) pair.
- Score below threshold → no match (seed_id absent from result).
- Greedy 1:1 — a seed already assigned is not stolen by a second spec.
- Robust to day_index reordering (output order ≠ input seed order).
- Empty inputs return empty dict.
"""

from __future__ import annotations

from app.agents._schemas.content_plan import PlanItemSpec
from app.services.seed_provenance import match_specs_to_seeds


def _spec(day: int, theme: str, idea: str) -> PlanItemSpec:
    return PlanItemSpec(day_index=day, theme=theme, idea=idea)


def _seed(sid: str, text: str, status: str = "pending") -> dict:
    return {"id": sid, "text": text, "status": status}


# ── basic match ───────────────────────────────────────────────────────────────


def test_single_match() -> None:
    specs = [_spec(1, "Football", "Fenerbahce match highlights reel")]
    seeds = [_seed("abc", "Fenerbahce game")]
    result = match_specs_to_seeds(specs, seeds)
    assert result == {0: "abc"}


def test_two_seeds_two_specs() -> None:
    specs = [
        _spec(3, "Morning fitness", "early morning run through London parks"),
        _spec(7, "Football", "Fenerbahce match day atmosphere"),
    ]
    seeds = [
        _seed("s1", "Fenerbahce game"),
        _seed("s2", "morning run in London"),
    ]
    result = match_specs_to_seeds(specs, seeds)
    # Each spec should match its seed regardless of seed list order.
    assert result[0] == "s2"
    assert result[1] == "s1"


# ── threshold floor ───────────────────────────────────────────────────────────


def test_disjoint_returns_empty() -> None:
    specs = [_spec(1, "Cooking", "pasta carbonara tutorial")]
    seeds = [_seed("x", "skydiving extreme sports")]
    result = match_specs_to_seeds(specs, seeds, threshold=0.30)
    assert result == {}


# ── greedy 1:1 — no double-assignment ─────────────────────────────────────────


def test_greedy_one_to_one() -> None:
    # Two specs that both overlap "morning run", but only one should win.
    specs = [
        _spec(1, "Morning run", "early morning run jog"),
        _spec(2, "Run", "morning jog routine"),
    ]
    seeds = [_seed("r1", "morning run")]
    result = match_specs_to_seeds(specs, seeds)
    # Exactly one spec gets the seed; the other is unmatched.
    assert len(result) == 1
    assert "r1" in result.values()


# ── reorder-robustness ────────────────────────────────────────────────────────


def test_reordered_specs_still_match() -> None:
    # Simulate parse() sorting by day_index: spec for seed[0] lands at index 1.
    specs = [
        _spec(5, "Football", "Fenerbahce match day highlights"),  # → seed s1
        _spec(2, "Morning", "morning run along the Thames"),  # → seed s2
    ]
    seeds = [
        _seed("s2", "morning run in London"),
        _seed("s1", "Fenerbahce game"),
    ]
    result = match_specs_to_seeds(specs, seeds)
    assert result[0] == "s1"
    assert result[1] == "s2"


# ── edge cases ────────────────────────────────────────────────────────────────


def test_empty_specs() -> None:
    assert match_specs_to_seeds([], [_seed("a", "idea")]) == {}


def test_empty_seeds() -> None:
    assert match_specs_to_seeds([_spec(1, "t", "idea")], []) == {}


def test_seed_missing_id_skipped() -> None:
    specs = [_spec(1, "Football", "Fenerbahce match day")]
    seeds = [{"text": "Fenerbahce game"}]  # no id key
    result = match_specs_to_seeds(specs, seeds)
    assert result == {}


def test_seed_missing_text_skipped() -> None:
    specs = [_spec(1, "Football", "Fenerbahce match day")]
    seeds = [{"id": "abc"}]  # no text key
    result = match_specs_to_seeds(specs, seeds)
    assert result == {}


def test_unmatched_spec_absent_from_result() -> None:
    specs = [
        _spec(1, "Football", "Fenerbahce match day"),
        _spec(2, "Cooking", "pasta carbonara recipe"),  # no matching seed
    ]
    seeds = [_seed("s1", "Fenerbahce game")]
    result = match_specs_to_seeds(specs, seeds)
    assert 0 in result
    assert 1 not in result
