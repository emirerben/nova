"""KRI-471 lane B: likelihood-based take assignment (pure, no aligner)."""

from __future__ import annotations

import itertools

from app.pipeline.take_assignment import (
    CandidateSpec,
    TakeSpec,
    assign_takes,
    margin_of,
    resolve_with_order,
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


# ── resolve_with_order (the creator's answer) ────────────────────────────────


def _resolve(takes, order, **kw):
    return resolve_with_order(takes, order, SONG_MS, **kw)


def test_sure_takes_keep_their_positions_and_are_not_creator_confirmed():
    out = _resolve([spec("A", 20, cand(10, 0.9)), spec("B", 20, cand(60, 0.9))], ["A", "B"])
    assert (out["A"].delta_ms, out["A"].basis, out["A"].confirmed) == (10_000, "aligner", False)
    assert out["B"].place == "pinned"


def test_one_fitting_candidate_is_decided_by_the_creator():
    takes = [
        spec("A", 10, cand(10, 0.9)),
        spec("X", 10, cand(30, 0.7), cand(80, 0.7)),
        spec("B", 10, cand(60, 0.9)),
    ]
    first = _resolve(takes, ["A", "X", "B"])
    assert (first["X"].delta_ms, first["X"].basis, first["X"].confirmed) == (
        30_000,
        "creator_position",
        True,
    )
    after = _resolve(takes, ["A", "B", "X"])
    assert after["X"].delta_ms == 80_000 and after["X"].basis == "creator_position"


def test_several_tied_fits_choose_the_nearest_unconfirmed():
    takes = [spec("A", 10, cand(10, 0.9)), spec("X", 10, cand(30, 0.7), cand(50, 0.7))]
    out = _resolve(takes, ["A", "X"])
    assert (out["X"].delta_ms, out["X"].basis, out["X"].confirmed) == (30_000, "tie_break", False)


def test_no_evidence_runs_stack_end_to_end_between_their_neighbours():
    takes = [
        spec("A", 10, cand(0, 0.9)),
        spec("S1", 5),
        spec("S2", 6),
        spec("B", 10, cand(40, 0.9)),
    ]
    out = _resolve(takes, ["A", "S1", "S2", "B"])
    s1, s2 = out["S1"], out["S2"]
    assert s1.place == s2.place == "stack" and s1.basis == "creator_stack" and s1.confirmed
    assert s1.likelihood == 0.0
    # A's last trusted frame is 9.7 s; the run starts where A's cover ends.
    assert 9_000 <= s1.delta_ms < 11_000
    assert s2.delta_ms == s1.delta_ms + 5_000
    assert s2.delta_ms + 6_000 <= 40_000 + 600  # ends where B's cover begins


def test_a_gap_too_small_runs_on_past_the_previous_take_into_the_next():
    takes = [spec("A", 10, cand(0, 0.9)), spec("S", 8), spec("B", 10, cand(12, 0.9))]
    out = _resolve(takes, ["A", "S", "B"])
    assert out["S"].place == "stack"
    # It hugs A's last trusted frame and overlaps B (the tiler trims), never starts
    # before A.
    assert out["S"].delta_ms == 9_400


def test_a_stacked_run_never_lands_on_the_wrong_side_of_its_anchors():
    # Probe: anchors a (10 s) and b (14 s), two 8 s stacked takes between them.
    takes = [
        spec("a", 4, cand(10, 0.9)),
        spec("n1", 8),
        spec("n2", 8),
        spec("b", 4, cand(14, 0.9)),
    ]
    out = _resolve(takes, ["a", "n1", "n2", "b"])
    a_end = 10_000 + 4_000
    assert out["n1"].place == "stack" and out["n1"].delta_ms >= a_end - 700
    # n2 would start past b's footage: it cannot precede b in the song, so B-roll.
    assert out["n2"].place == "broll" and out["n2"].reason == "no_room"
    for m in ("n1", "n2"):
        d = out[m].delta_ms
        assert d is None or d >= 10_000


def test_the_order_pass_prefers_a_candidate_that_keeps_the_cluster_inside_the_cap():
    # X's likelier candidate (200 s) is >120 s from the cluster; the in-cap one wins.
    takes = [spec("A", 10, cand(0, 0.9)), spec("X", 10, cand(200, 0.3), cand(40, 0.25))]
    out = _resolve(takes, ["A", "X"])
    assert out["X"].delta_ms == 40_000
    # With nothing in cap, fall back to every fit (likelihood decides).
    far = [spec("A", 10, cand(0, 0.9)), spec("X", 10, cand(200, 0.3), cand(180, 0.25))]
    assert _resolve(far, ["A", "X"])["X"].delta_ms == 200_000


def test_a_single_candidate_or_no_neighbours_is_not_claimed_by_the_creator():
    # Only one candidate exists: the aligner decided, not the creator's order.
    out = _resolve([spec("A", 10, cand(0, 0.9)), spec("W", 10, cand(40, 0.2))], ["A", "W"])
    assert (out["W"].basis, out["W"].confirmed) == ("aligner", False)
    # Two candidates but no known neighbour at all: the order decided nothing.
    out = _resolve([spec("W", 10, cand(40, 0.2), cand(90, 0.15))], ["W"])
    assert (out["W"].basis, out["W"].confirmed) == ("aligner", False)


def test_the_node_budget_keeps_the_best_assignment_found_so_far():
    # Two decisions where greedy is suboptimal; a budget that lets the DFS finish
    # one leaf must keep that better answer rather than reverting to greedy.
    takes = [
        spec("S", 40, cand(10, 0.9)),
        spec("P", 18, cand(10, 0.85), cand(60, 0.84)),
        spec("Q", 18, cand(32, 0.85)),
    ]
    exact = assign_takes(takes, SONG_MS)
    partial = assign_takes(takes, SONG_MS, node_cap=12)
    greedy = assign_takes(takes, SONG_MS, exact=False)
    assert len(partial.placed) >= len(greedy.placed)
    assert len(exact.placed) >= len(partial.placed)


def test_all_no_evidence_stack_from_the_first_lyric_line():
    out = _resolve([spec("X", 6), spec("Y", 6)], ["X", "Y"], first_line_ms=12_000)
    assert (out["X"].delta_ms, out["Y"].delta_ms) == (12_000, 18_000)


def test_a_stack_with_no_room_is_broll():
    out = resolve_with_order([spec("A", 8, cand(0, 0.9)), spec("X", 6)], ["A", "X"], 8_500)
    assert out["X"].place == "broll" and out["X"].delta_ms is None and out["X"].reason == "no_room"


def test_the_stack_cap_ignores_far_away_tied_takes():
    # A late chorus repeat far from the cluster must not starve the stack of room.
    takes = [
        spec("A", 8, cand(0, 0.9)),
        spec("S", 6),
        spec("Z", 8, cand(150, 0.5), cand(180, 0.5)),
    ]
    out = _resolve(takes, ["A", "S", "Z"])
    assert out["S"].place == "stack"
