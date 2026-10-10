"""KRI-561: the song-timeline answer, end to end through the lip-sync planner.

question -> creator's `placements` answer event -> `fold_song_orders` ->
`resolve_creator_placements` -> `resolved_song_takes_payload` -> `apply_resolved_song_takes`
-> `plan_lipsync_montage` (the exact chain the planner gate and the worker run).
"""

from __future__ import annotations

import pytest

from app.pipeline import lipsync_montage
from app.pipeline.lipsync_montage import plan_lipsync_montage
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
