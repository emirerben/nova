"""KRI-580: matching takes only inside the stretch of the song the creator filmed in.

The candidates below are the real ones from job 1c598df5 (a 209 s song, five takes): the aligner
found the sure takes at 44.3 s and 99.7 s, a weak guess at 88.6 s, a second place for the 99.7 s
take at 144.9 s (a repeated verse), and a weak guess at 185 s.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import settings
from app.pipeline.lipsync_montage import plan_lipsync_montage
from app.schemas.user_song import PlacementCandidate, SongAlignment, TakeAlignment
from app.services import song_range as sr
from app.services.song_order import build_song_order_question, takes_needing_order
from app.services.song_range import (
    SONG_RANGE_TOLERANCE_S,
    SongRange,
    parse_song_range,
    restrict_alignment_to_range,
    song_range_of,
)
from tests.pipeline.user_song_helpers import SONG_DURATION_S, SONG_ITEM_ID, analysis, take


def _row(media_id: str, *cands: tuple[float, float]) -> TakeAlignment:
    """A v3 row: ``(delta_s, likelihood)`` candidates; status derived like the aligner does."""
    ranked = sorted(cands, key=lambda c: -c[1])
    margin = 1.0 if len(ranked) == 1 else (ranked[0][1] - ranked[1][1]) / ranked[0][1]
    uncertain = margin < settings.song_align_ask_margin or (
        ranked[0][1] < settings.song_align_ask_likelihood
    )
    return TakeAlignment(
        media_id=media_id,
        status="ambiguous" if uncertain else "confident",
        delta_s=ranked[0][0],
        confidence=ranked[0][1],
        likelihood=ranked[0][1],
        margin=margin,
        method="lyrics",
        candidates=[
            PlacementCandidate(delta_s=d, likelihood=li, method="lyrics") for d, li in ranked
        ],
    )


def _real_job() -> SongAlignment:
    return SongAlignment(
        song_generation=7,
        takes={
            "D232": _row("D232", (44.303, 0.99)),
            "DD4A": _row("DD4A", (88.639, 0.29)),
            "C8FA": _row("C8FA", (99.66, 0.98), (144.9, 0.31), (146.9, 0.07)),
            "T9EE5": _row("T9EE5", (185.0, 0.14)),
            "T1184": TakeAlignment(media_id="T1184", status="unmatched"),
        },
    )


FILMED = SongRange(40.0, 110.0)


def test_no_range_returns_the_very_same_alignment() -> None:
    alignment = _real_job()
    assert restrict_alignment_to_range(alignment, None) is alignment


def test_a_take_with_nothing_outside_the_range_keeps_its_exact_row() -> None:
    alignment = _real_job()
    restricted = restrict_alignment_to_range(alignment, FILMED)
    assert restricted.takes["D232"] is alignment.takes["D232"]
    assert restricted.takes["DD4A"] is alignment.takes["DD4A"]
    assert restricted.takes["T1184"] is alignment.takes["T1184"]


def test_a_repeated_verse_outside_the_range_is_dropped_and_the_row_is_rederived() -> None:
    row = restrict_alignment_to_range(_real_job(), FILMED).takes["C8FA"]
    assert [c.delta_s for c in row.candidates] == [99.66]
    # One candidate left: it leads the (now absent) runner-up fully, and it is strong.
    assert row.margin == 1.0 and row.status == "confident"
    assert row.delta_s == 99.66 and row.alternates == []


def test_the_strongest_candidate_outside_the_range_does_not_win() -> None:
    """A take whose BEST guess is a repeat elsewhere falls back to its in-range guess."""
    alignment = SongAlignment(song_generation=7, takes={"A": _row("A", (150.0, 0.9), (60.0, 0.5))})
    row = restrict_alignment_to_range(alignment, FILMED).takes["A"]
    assert [c.delta_s for c in row.candidates] == [60.0]
    assert row.delta_s == 60.0 and row.likelihood == 0.5


def test_a_take_with_no_candidate_inside_the_range_has_no_evidence() -> None:
    row = restrict_alignment_to_range(_real_job(), FILMED).takes["T9EE5"]
    assert row.status == "unmatched" and row.delta_s is None and row.candidates == []
    assert row.candidates_or_legacy() == []


def test_removing_a_competing_repeat_makes_a_tied_take_certain() -> None:
    """The chorus case: two equal places, only one inside the stretch."""
    alignment = SongAlignment(song_generation=7, takes={"A": _row("A", (60.0, 0.8), (150.0, 0.8))})
    assert alignment.takes["A"].status == "ambiguous"
    row = restrict_alignment_to_range(alignment, FILMED).takes["A"]
    assert row.status == "confident" and row.delta_s == 60.0 and row.margin == 1.0


@pytest.mark.parametrize(
    ("delta", "kept"),
    [
        (40.0 - SONG_RANGE_TOLERANCE_S, True),  # a run-up before the stretch
        (40.0 - SONG_RANGE_TOLERANCE_S - 0.01, False),
        (110.0 + SONG_RANGE_TOLERANCE_S, True),
        (110.0 + SONG_RANGE_TOLERANCE_S + 0.01, False),
        (-2.0, False),  # a take that starts before the song, far from the stretch
    ],
)
def test_the_tolerance_edges(delta: float, kept: bool) -> None:
    alignment = SongAlignment(song_generation=7, takes={"A": _row("A", (delta, 0.9))})
    row = restrict_alignment_to_range(alignment, FILMED).takes["A"]
    assert (row.status != "unmatched") is kept


def test_legacy_rows_without_candidates_are_filtered_too() -> None:
    legacy = TakeAlignment(media_id="A", status="confident", delta_s=150.0, confidence=0.9)
    alignment = SongAlignment(song_generation=7, takes={"A": legacy})
    assert restrict_alignment_to_range(alignment, FILMED).takes["A"].status == "unmatched"


@pytest.mark.parametrize(
    ("start", "end", "ok"),
    [
        (40, 110, True),
        (0, 5, True),
        (-1, 10, False),
        (50, 50, False),
        (60, 50, False),
        (None, 50, False),
        (10, None, False),
        (float("nan"), 50, False),
        (10, float("inf"), False),
        (True, 50, False),
        ("10", 50, False),
    ],
)
def test_only_a_well_formed_range_parses(start, end, ok) -> None:
    assert (parse_song_range(start, end) is not None) is ok


def test_a_stored_range_is_ignored_while_the_kill_switch_is_off(monkeypatch) -> None:
    item = SimpleNamespace(song_range_start_s=40.0, song_range_end_s=110.0)
    assert song_range_of(item) == SongRange(40.0, 110.0)
    monkeypatch.setattr(settings, "song_filming_range_enabled", False)
    assert song_range_of(item) is None
    assert song_range_of(SimpleNamespace()) is None


# -- end to end on the real job's numbers ------------------------------------------------------


def test_the_question_for_the_real_job_stops_guessing_at_far_repeats() -> None:
    durations = {"D232": 29.8, "DD4A": 10.833, "C8FA": 7.667, "T9EE5": 14.267, "T1184": 33.5}
    ids = list(durations)
    plain = build_song_order_question(
        _real_job(), ids, question_id="q", durations=durations, song_duration_s=209.2
    )
    ranged = build_song_order_question(
        restrict_alignment_to_range(_real_job(), FILMED),
        ids,
        question_id="q",
        durations=durations,
        song_duration_s=209.2,
    )
    by_plain = {i.media_id: i for i in plain.items}
    by_ranged = {i.media_id: i for i in ranged.items}
    # The take the aligner could only guess at 185 s (a far repeat) no longer gets that position.
    assert by_plain["T9EE5"].song_start_s is not None and by_plain["T9EE5"].song_start_s > 150
    assert by_ranged["T9EE5"].song_start_s is None
    # The repeat at 144.9 s is no longer offered for the take that is sure at 99.7 s.
    assert any(c.delta_s > 140 for c in by_plain["C8FA"].candidates)
    assert all(c.delta_s < 110 for c in by_ranged["C8FA"].candidates)


def test_a_repeated_verse_no_longer_splits_the_group() -> None:
    """The failure this feature exists for.

    A, B and C were filmed back to back (20-56 s), but B's words ALSO match a repeat of the verse
    at 90 s, and that guess scores far higher. Without a range B lands at 90 s, far from A and C,
    and the planner keeps one side. With the stretch the creator filmed in, B lands where it
    belongs and the three stay one continuous run.
    """
    clips = [take("A", 12.0), take("B", 12.0), take("C", 12.0)]
    alignment = SongAlignment(
        song_generation=7,
        takes={
            "A": _row("A", (20.0, 0.95)),
            "B": _row("B", (90.0, 0.99), (32.0, 0.35)),
            "C": _row("C", (44.0, 0.95)),
        },
    )

    def run(al: SongAlignment) -> tuple[list[str], float]:
        plan = plan_lipsync_montage(clips, al, analysis(), plan_item_id=SONG_ITEM_ID)
        return [b["media_id"] for b in plan.song_receipt["placed"]], plan.snapshot.duration_s

    plain_ids, plain_s = run(alignment)
    ranged_ids, ranged_s = run(restrict_alignment_to_range(alignment, SongRange(15.0, 60.0)))
    assert set(plain_ids) != {"A", "B", "C"} and plain_s < 30  # the group split
    assert ranged_ids == ["A", "B", "C"] and ranged_s > 30  # one continuous run


def test_the_takes_still_needing_an_answer_shrink_when_the_range_removes_a_tie() -> None:
    alignment = SongAlignment(
        song_generation=7,
        takes={"A": _row("A", (60.0, 0.8), (150.0, 0.8)), "B": _row("B", (80.0, 0.95))},
    )
    ids = ["A", "B"]
    assert takes_needing_order(alignment, ids, {"A": 10.0, "B": 10.0}, 209.0) == ["A"]
    narrowed = restrict_alignment_to_range(alignment, FILMED)
    assert takes_needing_order(narrowed, ids, {"A": 10.0, "B": 10.0}, 209.0) == []


def test_module_constants_are_the_documented_ones() -> None:
    assert SONG_RANGE_TOLERANCE_S == 3.0
    assert sr.SongRange(1.0, 2.0).contains_start(-1.9)
    assert sr.REPEAT_MIN_LIKELIHOOD == 0.08
    assert sr.WEAK_TAKES_ASK_MIN == 2
    assert SONG_DURATION_S > 0


# -- KRI-650: the thread that never asked (job 76bf... / "Create Lipsync", 2026-10-13) --------
# Real candidates: sure placements at 44.3 s and 99.7 s, the chorus repeat near 145-154 s scored
# only 0.08-0.17, and three of the five takes were unsure. The question needs a 0.2 repeat, so it
# never fired and the creator was sent straight to the order card.

_NEVER_ASKED_DURATIONS = {"T29": 29.8, "T33": 33.5, "T10": 10.83, "T7": 7.67, "T14": 14.27}


def _never_asked() -> SongAlignment:
    return SongAlignment(
        song_generation=1,
        takes={
            "T29": _row("T29", (44.303, 0.9), (151.54, 0.164), (147.86, 0.105), (144.0, 0.08)),
            "T33": TakeAlignment(media_id="T33", status="ambiguous"),
            "T10": _row("T10", (88.6, 0.29)),
            "T7": _row("T7", (99.7, 0.95), (150.4, 0.17), (154.1, 0.16), (145.2, 0.08)),
            "T14": _row("T14", (185.0, 0.14)),
        },
    )


def test_the_job_that_never_asked_now_asks() -> None:
    ids = list(_NEVER_ASKED_DURATIONS)
    alignment = _never_asked()
    assert sr.takes_ambiguous_between_repeats(alignment, ids) == ["T29", "T7"]
    assert sr.should_ask_song_range(alignment, ids)


@pytest.mark.parametrize(
    ("statuses", "asks"),
    [
        (("confident", "confident", "confident"), False),
        (("ambiguous", "confident", "confident"), False),
        (("ambiguous", "unmatched", "confident"), True),
        (("unmatched", "unmatched", "unmatched"), True),
    ],
)
def test_several_unsure_takes_ask_even_without_a_visible_repeat(statuses, asks) -> None:
    takes = {
        f"M{i}": (
            _row(f"M{i}", (10.0 + 40 * i, 0.95))
            if status == "confident"
            else TakeAlignment(media_id=f"M{i}", status=status)
        )
        for i, status in enumerate(statuses)
    }
    alignment = SongAlignment(song_generation=1, takes=takes)
    assert sr.should_ask_song_range(alignment, list(takes)) is asks


def test_a_take_that_would_run_past_the_end_of_the_stretch_is_not_matched_there() -> None:
    # "I filmed seconds 1-50": a 29.8 s take starting at 44 s would play over seconds 44-74.
    stretch = SongRange(1.0, 50.0)
    row = restrict_alignment_to_range(
        _never_asked(), stretch, durations=_NEVER_ASKED_DURATIONS
    ).takes["T29"]
    assert row.status == "unmatched"
    assert row.candidates_or_legacy() == []


def test_a_take_that_fits_the_stretch_keeps_its_placement() -> None:
    stretch = SongRange(40.0, 110.0)
    restricted = restrict_alignment_to_range(
        _never_asked(), stretch, durations=_NEVER_ASKED_DURATIONS
    )
    assert restricted.takes["T29"].delta_s == pytest.approx(44.303)  # 44.3 + 29.8 = 74.1 <= 113
    assert restricted.takes["T7"].delta_s == pytest.approx(99.7)  # 99.7 + 7.67 <= 113
    assert restricted.takes["T7"].candidates_or_legacy()[0].delta_s == pytest.approx(99.7)
    assert len(restricted.takes["T7"].candidates_or_legacy()) == 1  # the chorus repeat is gone


def test_without_clip_lengths_only_the_start_is_checked() -> None:
    stretch = SongRange(1.0, 50.0)
    row = restrict_alignment_to_range(_never_asked(), stretch).takes["T29"]
    assert row.delta_s == pytest.approx(44.303)


def test_a_take_longer_than_the_whole_stretch_is_judged_by_where_it_starts() -> None:
    stretch = SongRange(40.0, 60.0)
    assert stretch.contains_take(44.0, 29.8)  # cannot fit anywhere; starts inside
    assert not stretch.contains_take(100.0, 29.8)


@pytest.mark.parametrize(
    ("delta", "duration", "fits"),
    [
        (10.0, 30.0, True),
        (22.0, 30.0, True),  # 52 <= 50 + 3
        (24.0, 30.0, False),  # 54 > 53
        (-2.5, 20.0, True),  # a little run-up before the stretch
        (-4.0, 20.0, False),
    ],
)
def test_the_whole_take_must_fit_with_the_run_up_slack(delta, duration, fits) -> None:
    assert SongRange(0.0, 50.0).contains_take(delta, duration) is fits
