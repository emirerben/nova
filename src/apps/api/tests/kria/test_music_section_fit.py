"""KRI-600: an auto-selected song section must never be longer than the footage can fill.

Prod 2026-10-11 (two threads): a creator with one 5 s clip never asked for music, the model
proposed licensed music, the server froze the track's whole 12-18 s "best section" as the
length owner, and the only answer offered was "Add longer footage".
"""

import pytest

from app.kria.planner import _fit_music_section_to_footage
from app.services.choice_questions import _source_coverage
from app.services.length_planning import build_length_plan

SECTION = {
    "status": "selected",
    "track_id": "t1",
    "title": "Instant Crush",
    "artist": "Daft Punk",
    "start_s": 130.0,
    "end_s": 148.0,
    "catalog_duration_s": 337.0,
    "beat_timestamps_s": [130.0, 130.5],
}


def _video(media_id: str, duration_s: float) -> dict:
    return {"media_id": media_id, "kind": "video", "duration_s": duration_s}


def test_section_longer_than_the_footage_is_trimmed_to_the_footage() -> None:
    fitted = _fit_music_section_to_footage(SECTION, [_video("a", 5.0)], selected_ids=set())

    assert fitted["start_s"] == 130.0
    assert fitted["end_s"] == 135.0
    assert fitted["track_id"] == "t1"


def test_capacity_is_the_sum_of_every_selected_video() -> None:
    fitted = _fit_music_section_to_footage(
        SECTION, [_video("a", 3.0), _video("b", 4.5), _video("c", 40.0)], selected_ids={"a", "b"}
    )

    assert fitted["end_s"] == 137.5


def test_section_that_already_fits_is_left_alone() -> None:
    assert (
        _fit_music_section_to_footage(SECTION, [_video("a", 30.0)], selected_ids=set()) == SECTION
    )


def test_a_non_selected_section_is_left_alone() -> None:
    fallback = {"status": "fallback_footage"}

    assert (
        _fit_music_section_to_footage(fallback, [_video("a", 5.0)], selected_ids=set()) == fallback
    )


def test_stills_make_capacity_unknowable_so_the_section_is_not_trimmed() -> None:
    rows = [_video("a", 5.0), {"media_id": "p", "kind": "image", "duration_s": None}]

    assert _fit_music_section_to_footage(SECTION, rows, selected_ids=set()) == SECTION


def test_unparseable_footage_durations_do_not_trim() -> None:
    rows = [_video("a", 5.0), {"media_id": "b", "kind": "video", "duration_s": None}]

    assert _fit_music_section_to_footage(SECTION, rows, selected_ids=set()) == SECTION


def test_input_section_is_never_mutated() -> None:
    before = dict(SECTION)
    _fit_music_section_to_footage(SECTION, [_video("a", 5.0)], selected_ids=set())

    assert SECTION == before


def _plan(section: dict) -> dict:
    strategy = {
        "edit_format": "montage",
        "audio_strategy": "licensed_music",
        "selected_music_section": section,
    }
    snapshot = {"clip_assignments": [_video("a", 5.0)]}
    return build_length_plan(strategy, snapshot, selected_ids=["a"], licensed_music=section)


def test_incident_before_the_fit_the_plan_dead_ends() -> None:
    plan = _plan(SECTION)

    assert [c["code"] for c in plan["conflicts"]] == ["insufficient_capacity"]


@pytest.mark.parametrize("section_end", [148.0, 142.0])
def test_incident_after_the_fit_there_is_no_conflict_and_no_card(section_end: float) -> None:
    fitted = _fit_music_section_to_footage(
        {**SECTION, "end_s": section_end}, [_video("a", 5.0)], selected_ids=set()
    )
    plan = _plan(fitted)

    assert plan["conflicts"] == []
    strategy = {"audio_strategy": "licensed_music", "selected_music_section": fitted}
    assert _source_coverage(strategy, None, [], None, snapshot={"length_plan": plan}) is None
