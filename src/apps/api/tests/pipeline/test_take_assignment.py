"""KRI-471 lane B: likelihood-based take assignment (pure, no aligner)."""

from __future__ import annotations

import itertools

from app.pipeline.take_assignment import (
    CandidateSpec,
    TakeSpec,
    assign_takes,
    margin_of,
)

SONG_MS = 240_000


def cand(delta_s: float, likelihood: float, match: tuple[float, float] | None = None):
    return CandidateSpec(
        int(delta_s * 1000),
        likelihood,
        (int(match[0] * 1000), int(match[1] * 1000)) if match else None,
        "lyrics",
    )


def spec(media_id: str, duration_s: float, *cands: CandidateSpec) -> TakeSpec:
    return TakeSpec(media_id, int(duration_s * 1000), tuple(cands))


def test_a_clear_take_is_placed_with_its_likelihood_and_no_question():
    result = assign_takes([spec("A", 20, cand(10, 0.9))], SONG_MS)
    claim = result.placed["A"]
    assert (claim.delta_ms, claim.likelihood, claim.margin, claim.basis) == (
        10_000,
        0.9,
        1.0,
        "aligner",
    )
    assert result.ask == {} and result.unplaced == {}


def test_no_status_gate_a_weak_candidate_is_placed_and_flagged_weak():
    result = assign_takes([spec("W", 20, cand(10, 0.2))], SONG_MS)
    assert "W" in result.placed
    assert result.ask == {"W": "weak"}


def test_takes_without_candidates_are_unplaced_and_ask_no_evidence():
    result = assign_takes([spec("N", 20), spec("A", 20, cand(10, 0.9))], SONG_MS)
    assert result.unplaced == {"N": "no_evidence"}
    assert result.ask == {"N": "no_evidence"}
    assert set(result.placed) == {"A"}


def test_conflicting_claims_higher_likelihood_wins_other_falls_to_its_second_candidate():
    strong = spec("S", 20, cand(10, 0.9))
    weak = spec("W", 20, cand(10, 0.5), cand(60, 0.3))
    result = assign_takes([weak, strong], SONG_MS)
    assert result.placed["S"].delta_ms == 10_000
    assert result.placed["W"].delta_ms == 60_000
    assert result.ask["W"] == "weak"  # fell to a lower candidate


def test_a_conflict_with_no_second_candidate_leaves_the_take_unplaced():
    strong = spec("S", 20, cand(10, 0.9))
    weak = spec("W", 15, cand(12, 0.5))  # inside S: not a staggered overlap
    result = assign_takes([strong, weak], SONG_MS)
    assert set(result.placed) == {"S"}
    assert result.unplaced == {"W": "conflict"}
    assert result.ask == {"W": "weak"}


def test_staggered_overlap_is_not_a_conflict_but_containment_is():
    a = spec("A", 20, cand(10, 0.9))
    b = spec("B", 20, cand(25, 0.9))  # re-sung stretch: adds 15 s past A
    inside = spec("C", 5, cand(15, 0.9))
    result = assign_takes([a, b, inside], SONG_MS)
    assert {"A", "B"} <= set(result.placed)
    assert "C" not in result.placed or "A" not in result.placed


def test_a_chorus_repeat_tie_prefers_the_repeat_inside_the_cluster():
    takes = [
        spec("T1", 10, cand(0.7, 0.55)),
        spec("T4", 10, cand(15.8, 0.56)),
        spec("T8", 20, cand(159.5, 0.8), cand(145.8, 0.8), cand(63.5, 0.8), cand(49.8, 0.8)),
    ]
    result = assign_takes(takes, SONG_MS)
    assert result.placed["T8"].delta_ms in (49_800, 63_500)
    assert result.placed["T8"].basis == "tie_break"
    assert result.ask["T8"] == "tie"
    assert margin_of(takes[2].candidates) == 0.0


def test_exact_ties_pick_the_earliest_when_nothing_else_distinguishes():
    only = spec("T", 10, cand(80, 0.7), cand(30, 0.7), cand(130, 0.7))
    result = assign_takes([only], SONG_MS)
    assert result.placed["T"].delta_ms == 30_000


def test_a_near_tie_is_a_tie_the_cluster_decides_not_the_slightly_higher_score():
    takes = [
        spec("A", 10, cand(10, 0.9)),
        spec("R", 10, cand(160, 0.60), cand(30, 0.52)),  # 0.52 >= 0.85 * 0.60
    ]
    result = assign_takes(takes, SONG_MS)
    assert result.placed["R"].delta_ms == 30_000
    assert result.ask["R"] == "tie"


def test_a_clearly_weaker_candidate_never_beats_the_best_even_nearer_the_cluster():
    takes = [spec("A", 10, cand(10, 0.9)), spec("R", 10, cand(160, 0.8), cand(30, 0.3))]
    result = assign_takes(takes, SONG_MS)
    assert result.placed["R"].delta_ms == 160_000


def test_exact_search_beats_greedy_when_the_strongest_take_blocks_two_others():
    # S (0.9) overlaps both P and Q, which do not overlap each other: greedy takes S
    # (0.9); the exact answer places P and Q (0.85 + 0.85 = 1.7).
    takes = [
        spec("S", 40, cand(10, 0.9)),
        spec("P", 18, cand(10, 0.85)),
        spec("Q", 18, cand(32, 0.85)),
    ]
    greedy = assign_takes(takes, SONG_MS, exact=False)
    exact = assign_takes(takes, SONG_MS)
    assert set(greedy.placed) == {"S"}
    assert set(exact.placed) == {"P", "Q"}


def test_exact_and_greedy_agree_when_nothing_competes():
    takes = [spec(f"T{i}", 10, cand(i * 20, 0.5 + i / 20)) for i in range(5)]
    assert assign_takes(takes, SONG_MS).placed == assign_takes(takes, SONG_MS, exact=False).placed


def test_the_node_budget_falls_back_to_the_greedy_answer():
    takes = [
        spec("S", 40, cand(10, 0.9)),
        spec("P", 18, cand(10, 0.85)),
        spec("Q", 18, cand(32, 0.85)),
    ]
    capped = assign_takes(takes, SONG_MS, node_cap=1)
    assert set(capped.placed) == set(assign_takes(takes, SONG_MS, exact=False).placed)


def test_placed_claims_never_contain_each_other():
    takes = [
        spec(f"T{i}", 12, *(cand(d, 0.6) for d in (10 + 3 * i, 50 + 7 * i, 90))) for i in range(6)
    ]
    placed = list(assign_takes(takes, SONG_MS).placed.values())
    for a, b in itertools.combinations(placed, 2):
        overlap = min(a.claim_end_ms, b.claim_end_ms) - max(a.claim_start_ms, b.claim_start_ms)
        if overlap > 1000:
            first, second = sorted((a, b), key=lambda c: c.claim_start_ms)
            assert second.claim_start_ms > first.claim_start_ms
            assert second.claim_end_ms - first.claim_end_ms >= 1000


def test_a_match_range_shrinks_the_claim_so_neighbours_fit():
    trimmed = spec("A", 30, cand(10, 0.9, match=(8, 20)))  # claims song 18-30
    other = spec("B", 30, cand(30, 0.9))  # claims 30-60
    result = assign_takes([trimmed, other], SONG_MS)
    assert set(result.placed) == {"A", "B"}
    assert (result.placed["A"].claim_start_ms, result.placed["A"].claim_end_ms) == (18_000, 30_000)


def test_candidates_that_cannot_cover_a_segment_are_not_claimed():
    song_ms = 12_000
    only = spec("T", 20, cand(11.5, 0.9))  # only 0.5 s of song left at the end
    result = assign_takes([only], song_ms)
    assert result.unplaced == {"T": "too_short"}


def test_pinned_takes_are_placed_first_and_everything_else_avoids_them():
    takes = [spec("U", 20, cand(10, 0.4), cand(80, 0.4)), spec("S", 20, cand(10, 0.9))]
    result = assign_takes(takes, SONG_MS, pinned={"U": 10_000})
    assert result.placed["U"].basis == "pinned"
    assert "S" not in result.placed and result.unplaced["S"] == "conflict"
    assert "U" not in result.ask


def test_excluded_takes_stay_unplaced_with_the_callers_reason():
    result = assign_takes(
        [spec("X", 20, cand(10, 0.9))], SONG_MS, exclude={"X": "no_fitting_position"}
    )
    assert result.unplaced == {"X": "no_fitting_position"}
