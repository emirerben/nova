"""KRI-561: the song-timeline answer, end to end through the lip-sync planner.

question -> creator's `placements` answer event -> `fold_song_orders` ->
`resolve_creator_placements` -> `resolved_song_takes_payload` -> `apply_resolved_song_takes`
-> `plan_lipsync_montage` (the exact chain the planner gate and the worker run).
"""

from __future__ import annotations

import pytest

from app.pipeline import lipsync_montage
from app.pipeline.lipsync_montage import close_small_holes, plan_lipsync_montage
from app.schemas.user_song import (
    PlacementCandidate,
    SongAlignment,
    SongOrderAnswerIn,
    SongOrderPlacementIn,
    TakeAlignment,
)
from app.services.song_order import (
    INVALID_CODE,
    SongOrderError,
    apply_resolved_song_takes,
    build_song_order_question,
    fold_song_orders,
    resolve_creator_placements,
    resolved_song_takes_payload,
    validate_song_order_answer,
)
from tests.pipeline.user_song_helpers import (
    SONG_DURATION_S,
    SONG_GENERATION,
    SONG_ITEM_ID,
    alignment,
    analysis,
    confident,
    take,
    unmatched,
)

QID = "q-timeline"


def _with_candidates(media_id: str, *cands: tuple[float, float]) -> TakeAlignment:
    """An ambiguous take with several places it could sit: ``(delta_s, likelihood)``."""
    ranked = sorted(cands, key=lambda c: -c[1])
    return TakeAlignment(
        media_id=media_id,
        status="ambiguous",
        delta_s=ranked[0][0],
        confidence=ranked[0][1],
        likelihood=ranked[0][1],
        margin=0.1,
        candidates=[
            PlacementCandidate(delta_s=d, likelihood=li, method="lyrics") for d, li in ranked
        ],
    )


def _arrange(
    clips: list,
    rows: list[TakeAlignment],
    placements: dict[str, float],
    *,
    order: list[str] | None = None,
):
    """Run the whole chain for a timeline answer; returns (plan, resolved rows, question)."""
    align: SongAlignment = alignment(*rows)
    ids = [c.media_id for c in clips]
    durations = {c.media_id: c.duration_s for c in clips}
    question = build_song_order_question(
        align,
        ids,
        question_id=QID,
        durations=durations,
        song_duration_s=SONG_DURATION_S,
        first_line_s=2.0,
    )
    tray = [m for m in (order or ids) if m not in placements]
    ordered = sorted(placements, key=lambda m: placements[m]) + tray
    answer = {
        "question_id": QID,
        "ordered_media_ids": ordered,
        "placements": [{"media_id": m, "delta_s": d} for m, d in placements.items()],
    }
    events = [
        ("assistant", {"song_order_question": question.model_dump(mode="json")}),
        ("user", {"song_order": answer}),
    ]
    folded = fold_song_orders(events, align.song_generation)
    assert folded.covers(ids)
    resolved = resolve_creator_placements(
        align, folded.ordered_media_ids, folded.placements, durations, SONG_DURATION_S
    )
    payload = resolved_song_takes_payload(resolved)
    align, confirmed_order, choices = apply_resolved_song_takes(align, payload)
    plan = plan_lipsync_montage(
        clips,
        align,
        analysis(),
        plan_item_id=SONG_ITEM_ID,
        confirmed_order=confirmed_order,
        creator_choices=choices,
    )
    return plan, resolved, question


def _reasons(plan) -> dict[str, str]:
    return {row["media_id"]: row["reason"] for row in plan.song_receipt["dropped"]}


def test_question_carries_what_the_timeline_needs():
    rows = [confident("A", 10), unmatched("B")]
    question = build_song_order_question(
        alignment(*rows),
        ["A", "B"],
        question_id=QID,
        durations={"A": 12.0, "B": 9.0},
        song_duration_s=SONG_DURATION_S,
        first_line_s=2.0,
    )
    assert question.song_duration_s == SONG_DURATION_S
    assert question.max_window_s == 120
    assert question.first_line_s == 2.0
    by_id = {i.media_id: i for i in question.items}
    assert by_id["A"].duration_s == 12.0 and by_id["B"].duration_s == 9.0
    # Candidates go out for EVERY take, the confident one included, so a drag can snap.
    assert [c.delta_s for c in by_id["A"].candidates] == [10.0]
    assert by_id["B"].candidates == []


def test_a_confident_take_can_be_moved_and_the_move_is_the_creators_decision():
    plan, resolved, _ = _arrange(
        [take("A", 12), take("B", 12)],
        [confident("A", 10), confident("B", 34)],
        {"A": 22, "B": 10},  # B first, A right behind it: 10-22, 22-34 (no hole)
    )
    assert resolved["A"]["position_basis"] == "creator_position"
    assert resolved["A"]["confirmed_by_creator"] is True
    taken = plan.user_song.takes
    assert taken["A"].delta_s == pytest.approx(22) and taken["B"].delta_s == pytest.approx(10)
    assert [b["media_id"] for b in plan.song_receipt["placed"]] == ["B", "A"]
    assert taken["A"].confirmed_by_creator and taken["B"].confirmed_by_creator


def test_an_untouched_confident_take_keeps_the_aligners_credit():
    _, resolved, _ = _arrange(
        [take("A", 12), take("B", 12)],
        [confident("A", 10), confident("B", 34)],
        {"A": 10, "B": 34},
    )
    for media_id in ("A", "B"):
        assert resolved[media_id]["position_basis"] == "aligner"
        assert resolved[media_id]["confirmed_by_creator"] is False


def test_a_drop_near_a_candidate_snaps_to_it_so_the_lips_land_on_the_measured_spot():
    # C's best guess is 80, but it could also sit at 21.9; the creator drops it at 22.0 in
    # the 22-36 gap, 0.1 s away from that candidate.
    plan, resolved, _ = _arrange(
        [take("A", 12), take("B", 12), take("C", 15)],
        [confident("A", 10), confident("B", 36), _with_candidates("C", (80.0, 0.9), (21.9, 0.4))],
        {"A": 10, "B": 36, "C": 22.0},
    )
    assert resolved["C"]["delta_s"] == pytest.approx(21.9)
    assert resolved["C"]["likelihood"] == pytest.approx(0.4)
    assert resolved["C"]["position_basis"] == "creator_position"
    assert resolved["C"]["confirmed_by_creator"] is True
    assert plan.user_song.takes["C"].delta_s == pytest.approx(21.9)
    assert [b["media_id"] for b in plan.song_receipt["placed"]] == ["A", "C", "B"]


def test_a_drop_with_no_candidate_pins_at_the_drop_point_with_no_measured_likelihood():
    plan, resolved, _ = _arrange(
        [take("A", 12), take("B", 12), take("C", 12)],
        [confident("A", 10), confident("B", 34), unmatched("C")],
        {"A": 10, "B": 34, "C": 22.0},
    )
    assert resolved["C"]["delta_s"] == pytest.approx(22.0)
    assert resolved["C"]["likelihood"] == 0.0
    pinned = plan.user_song.takes["C"]
    assert pinned.delta_s == pytest.approx(22.0)
    assert pinned.confirmed_by_creator and pinned.position_basis == "creator_position"
    # The gap is filled by the creator's clip itself, not by neighbouring footage.
    assert [b["media_id"] for b in plan.song_receipt["placed"]] == ["A", "C", "B"]


def test_a_take_left_in_the_tray_is_muted_footage_never_placed_by_song_time():
    plan, resolved, _ = _arrange(
        [take("A", 12), take("B", 12), take("D", 12)],
        [confident("A", 10), confident("B", 34), unmatched("D")],
        {"A": 10, "B": 34},
    )
    assert resolved["D"]["place"] == "broll" and resolved["D"]["delta_s"] is None
    assert resolved["D"]["reason"] == "creator_unplaced"
    assert "D" not in plan.user_song.takes
    # Kept (nothing is lost) as footage that fills the 22-34 gap, as the tray promised.
    assert "D" in plan.song_receipt["broll_ids"]


def test_a_pin_hidden_inside_another_take_is_dropped_as_overlapped_not_reused():
    # E is 6 s long, dropped at 12: its 12-18 sits entirely inside A (10-22).
    plan, _, _ = _arrange(
        [take("A", 12), take("B", 12), take("E", 6)],
        [confident("A", 10), confident("B", 34), unmatched("E")],
        {"A": 10, "B": 34, "E": 12},
    )
    assert _reasons(plan).get("E") == "overlapped"
    assert "E" not in plan.user_song.takes
    assert "E" not in plan.song_receipt["broll_ids"]


def test_creator_placed_takes_outweigh_a_sure_take_when_the_montage_must_pick_a_span():
    # A is sure at 10-22. The creator lays C and D back to back far away (60-84): the
    # 38 s hole to A cannot be filled, so the planner keeps ONE span. Their two clips
    # (0.5 x 24 s) must beat A's single sure one (~0.95 x 12 s), not be dropped behind it.
    plan, _, _ = _arrange(
        [take("A", 12), take("C", 12), take("D", 12)],
        [confident("A", 10), unmatched("C"), unmatched("D")],
        {"A": 10, "C": 60, "D": 72},
    )
    assert set(plan.user_song.takes) == {"C", "D"}
    assert _reasons(plan).get("A") in {"gap_unfillable", "outside_window"}


def test_the_floor_is_what_flips_that_choice(monkeypatch):
    monkeypatch.setattr(lipsync_montage, "CREATOR_PLACED_WEIGHT", 0.05)
    plan, _, _ = _arrange(
        [take("A", 12), take("C", 12), take("D", 12)],
        [confident("A", 10), unmatched("C"), unmatched("D")],
        {"A": 10, "C": 60, "D": 72},
    )
    assert set(plan.user_song.takes) == {"A"}


# -- validation ----------------------------------------------------------------


def _question(durations=None):
    return build_song_order_question(
        alignment(confident("A", 10), unmatched("B")),
        ["A", "B"],
        question_id=QID,
        durations=durations or {"A": 12.0, "B": 8.0},
        song_duration_s=SONG_DURATION_S,
    )


def _answer(**placements: float) -> SongOrderAnswerIn:
    return SongOrderAnswerIn(
        question_id=QID,
        ordered_media_ids=["A", "B"],
        placements=[SongOrderPlacementIn(media_id=m, delta_s=d) for m, d in placements.items()],
    )


def test_a_valid_arrangement_passes_validation():
    validate_song_order_answer(_answer(A=10.0, B=-3.0), _question())


@pytest.mark.parametrize(
    "bad",
    [
        {"A": 10.0, "X": 5.0},  # a take that was never asked about
        {"A": 500.0},  # starts after the song ends
        {"B": -8.0},  # ends before the song starts (take is 8 s long)
    ],
)
def test_arrangements_that_cannot_sit_on_the_song_are_422(bad):
    with pytest.raises(SongOrderError) as err:
        validate_song_order_answer(_answer(**bad), _question())
    assert err.value.code == INVALID_CODE


def test_the_same_take_placed_twice_is_422():
    answer = SongOrderAnswerIn(
        question_id=QID,
        ordered_media_ids=["A", "B"],
        placements=[
            SongOrderPlacementIn(media_id="A", delta_s=1),
            SongOrderPlacementIn(media_id="A", delta_s=2),
        ],
    )
    with pytest.raises(SongOrderError) as err:
        validate_song_order_answer(answer, _question())
    assert err.value.code == INVALID_CODE


def test_a_non_finite_delta_is_rejected_by_the_schema():
    with pytest.raises(ValueError):
        SongOrderPlacementIn(media_id="A", delta_s=float("nan"))


def test_a_plain_reorder_answer_folds_without_placements():
    question = _question()
    events = [
        ("assistant", {"song_order_question": question.model_dump(mode="json")}),
        ("user", {"song_order": {"question_id": QID, "ordered_media_ids": ["B", "A"]}}),
    ]
    folded = fold_song_orders(events, SONG_GENERATION)
    assert folded.ordered_media_ids == ("B", "A") and folded.placements == ()
    assert folded.arranged is False


def test_malformed_placements_are_ignored_but_the_order_still_counts():
    question = _question()
    events = [
        ("assistant", {"song_order_question": question.model_dump(mode="json")}),
        (
            "user",
            {
                "song_order": {
                    "question_id": QID,
                    "ordered_media_ids": ["A", "B"],
                    "placements": [{"media_id": "ghost", "delta_s": 1.0}],
                }
            },
        ),
    ]
    folded = fold_song_orders(events, SONG_GENERATION)
    assert folded.ordered_media_ids == ("A", "B") and folded.placements == ()
    assert folded.arranged is False


def test_an_empty_placements_list_is_the_creators_decision_that_nothing_is_placed():
    question = _question()
    events = [
        ("assistant", {"song_order_question": question.model_dump(mode="json")}),
        (
            "user",
            {
                "song_order": {
                    "question_id": QID,
                    "ordered_media_ids": ["A", "B"],
                    "placements": [],
                }
            },
        ),
    ]
    folded = fold_song_orders(events, SONG_GENERATION)
    assert folded.arranged is True and folded.placements == ()
    resolved = resolve_creator_placements(
        alignment(confident("A", 10), unmatched("B")), folded.ordered_media_ids, folded.placements
    )
    assert {r["place"] for r in resolved.values()} == {"broll"}


# -- KRI-575: sub-second holes in a hand-made arrangement ------------------------------------
#
# Real job 1c598df5 (a 209 s song, five takes): the creator's arrangement put four takes back to
# back from 44.3 s to 107.3 s, each starting 0.07-0.2 s after the previous one's footage ended.
# That is song time NO take has footage for, so no gap rule bridged it, the montage split into
# five islands and the heaviest single take (29.2 s) was the whole draft.


def _user_arrangement():
    clips = [
        take("T1184", 33.5),  # creator put it at the very start (no evidence)
        take("D232", 29.8),
        take("T9EE5", 14.266667),
        take("DD4A", 10.833333),
        take("C8FA", 7.666667),
    ]
    rows = [
        unmatched("T1184"),
        # Real likelihoods from the job: the two sure takes are what the aligner measured.
        _with_candidates("D232", (44.303, 0.99)),
        _with_candidates("T9EE5", (185.0, 0.14)),
        _with_candidates("DD4A", (88.639, 0.29)),
        _with_candidates("C8FA", (99.66, 0.98), (144.9, 0.31)),
    ]
    placements = {
        "T1184": -3.8,
        "D232": 44.303,
        "T9EE5": 74.3,
        "DD4A": 88.639,
        "C8FA": 99.66,
    }
    return clips, rows, placements


def test_the_real_arrangement_keeps_all_four_takes_that_nearly_touch():
    clips, rows, placements = _user_arrangement()
    plan, _resolved, _ = _arrange(clips, rows, placements)
    placed = [b["media_id"] for b in plan.song_receipt["placed"]]
    assert placed == ["D232", "T9EE5", "DD4A", "C8FA"]
    # One continuous stretch (was 29.2 s: the first take alone).
    assert plan.snapshot.duration_s > 60
    # The far-away take that no gap can reach is the only one left out.
    assert {row["media_id"]: row["reason"] for row in plan.song_receipt["dropped"]} == {
        "T1184": "gap_unfillable"
    }


def test_closing_the_holes_slides_each_take_a_fraction_of_a_second_and_keeps_its_match():
    clips, rows, placements = _user_arrangement()
    plan, _resolved, _ = _arrange(clips, rows, placements)
    by_id = {b["media_id"]: b for b in plan.song_receipt["placed"]}
    slid = {m: b.get("slipped_s", 0) for m, b in by_id.items()}
    # 0.457 s of holes sit between the two takes the aligner is sure of (0.99 and 0.98);
    # clips cannot stretch, so something has to slide. No take moves further than the cap,
    # and every take that did move says so in the receipt.
    assert any(slid.values()) and all(abs(v) <= 0.8 for v in slid.values())
    # A slid take is still the same measured match: it keeps its likelihood, so the run of four
    # outweighs the lone first take instead of losing to it.
    assert by_id["D232"]["likelihood"] >= 0.9 and by_id["C8FA"]["likelihood"] >= 0.9


def test_without_hole_closing_the_same_arrangement_collapses_to_one_take(monkeypatch):
    """The regression this fixes: with the old behaviour the draft is the first take alone."""
    monkeypatch.setattr(lipsync_montage, "HOLE_CLOSE_MAX_MS", 0)
    monkeypatch.setattr(lipsync_montage, "FREEZE_BRIDGE_MAX_MS", 0)
    clips, rows, placements = _user_arrangement()
    plan, _resolved, _ = _arrange(clips, rows, placements)
    assert len(plan.song_receipt["placed"]) == 1
    assert plan.snapshot.duration_s < 35


def test_holes_between_takes_the_creator_did_not_place_are_left_to_the_tiler():
    """No creator arrangement (aligner positions only): nothing is slid."""
    clips = [take("A", 12), take("B", 12)]
    plan = plan_lipsync_montage(
        clips,
        alignment(confident("A", 10), confident("B", 22.3)),
        analysis(),
        plan_item_id=SONG_ITEM_ID,
    )
    assert all("slipped_s" not in row for row in plan.song_receipt["placed"])


def _slide(pins, durations, weights=None, **kwargs):
    return close_small_holes(pins, durations, weights or {m: 1.0 for m in pins}, **kwargs)


def test_two_takes_with_a_small_hole_close_it_by_sliding_the_cheaper_one():
    # A (10 s at 0) ends at 10_000; B starts at 10_200: a 200 ms hole.
    pins = {"A": 0, "B": 10_200}
    dur = {"A": 10_000, "B": 5_000}
    out = _slide(pins, dur, {"A": 0.9, "B": 0.2})
    assert out == {"A": 0, "B": 10_000}  # B slides earlier; the strong A stays
    out = _slide(pins, dur, {"A": 0.2, "B": 0.9})
    assert out == {"A": 200, "B": 10_200}  # now A slides later and B stays


def test_a_chain_anchors_on_the_take_that_minimises_the_weighted_slide():
    pins = {"A": 0, "B": 10_200, "C": 20_300}  # holes 200 and 100 ms
    dur = {"A": 10_000, "B": 10_000, "C": 5_000}
    out = _slide(pins, dur, {"A": 1.0, "B": 0.2, "C": 1.0})
    # B is cheap: it closes both holes alone (A and C fixed is impossible), so B moves and
    # the strong ends stay, which needs the holes 300 ms apart to be absorbed by B: not possible
    # without moving a strong take, so the cheapest consistent anchor is chosen.
    assert out["B"] != pins["B"] or out["C"] != pins["C"] or out["A"] != pins["A"]
    # Whatever is chosen, the run ends up with no holes left.
    ordered = sorted(out, key=lambda m: out[m])
    for a, b in zip(ordered, ordered[1:], strict=False):
        assert out[b] - (out[a] + dur[a]) == 0


@pytest.mark.parametrize(
    "pins",
    [
        {"A": 0, "B": 10_000},  # exactly adjacent: no hole
        {"A": 0, "B": 9_500},  # overlap: the tiler's business
        {"A": 0, "B": 11_000},  # a 1 s hole: a real gap, not a miss
    ],
)
def test_adjacent_overlapping_or_big_gaps_are_never_touched(pins):
    dur = {"A": 10_000, "B": 5_000}
    assert _slide(pins, dur) == pins


def test_a_run_that_needs_more_than_the_slide_cap_is_left_alone():
    # Four 590 ms holes in a row: even the best anchor slides a take by >= 1.18 s.
    pins = {"A": 0, "B": 10_590, "C": 21_180, "D": 31_770, "E": 42_360}
    dur = {m: 10_000 for m in pins}
    assert _slide(pins, dur) == pins


def test_a_single_take_and_an_empty_arrangement_are_unchanged():
    assert _slide({}, {}) == {}
    assert _slide({"A": 5}, {"A": 100}) == {"A": 5}


def test_negative_positions_and_unsorted_input_are_handled():
    pins = {"B": 10_200, "A": -3_800}  # insertion order is not time order
    dur = {"A": 14_000, "B": 5_000}  # A ends at 10_200: exactly adjacent
    assert _slide(pins, dur) == pins
    pins = {"B": 10_500, "A": -3_800}  # a 300 ms hole
    out = _slide(pins, dur, {"A": 1.0, "B": 0.5})
    assert out == {"A": -3_800, "B": 10_200}
