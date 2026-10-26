"""KRI-847: the approved song window does not reject a ceiling it is shorter than.

``_effective_music_window`` refuses a frozen song section shorter than the creator's length unless
it is within ten percent of it ("Approved song section cannot satisfy the requested length"). For
"under 20 seconds", a 12 s section is not a shortfall. Failure modes written first:

1. A frozen section shorter than the ceiling fails the render with that ValueError.
2. A song (unfrozen path) shorter than the ceiling fails with "Selected song window is no
   longer available".
3. The ceiling flag leaks onto an exact duration: the editor's persisted
   ``music_window_video_duration_s`` is a concrete length, never a ceiling.
4. An exact length stops refusing a section that is too short (guard).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.tasks.generative_build as gb


def _frozen_track(start_s: float = 12.25, end_s: float = 31.75):
    return SimpleNamespace(
        id="00000000-0000-0000-0000-000000000111",
        duration_s=60.0,
        beat_timestamps_s=[0.0, start_s, end_s],
        track_config={
            "best_start_s": start_s,
            "best_end_s": end_s,
            "_approved_frozen_music_window": True,
        },
    )


def _window(track, requested_s, bound="exact"):
    kwargs = {} if bound == "exact" else {"requested_duration_bound": bound}
    return gb._effective_music_window(
        track,
        requested_start_s=0.0,
        requested_duration_s=requested_s,
        fallback_footage_s=3.0,
        **kwargs,
    )


# --------------------------------------------------------------- the frozen (approved) section


def test_a_frozen_section_shorter_than_the_ceiling_is_kept_whole():
    window = _window(_frozen_track(), 30.0, "at_most")  # 19.5 s section under "under 30 s"

    assert window["validated"] is True
    assert window["duration_s"] == pytest.approx(19.5)
    assert window["start_s"] == pytest.approx(12.25)


def test_guard_the_same_section_is_refused_for_an_exact_thirty_seconds():
    with pytest.raises(ValueError, match="cannot satisfy"):
        _window(_frozen_track(), 30.0)


def test_a_frozen_section_longer_than_the_ceiling_is_trimmed_to_it():
    window = _window(_frozen_track(), 10.0, "at_most")

    assert window["duration_s"] == pytest.approx(10.0)
    assert window["end_s"] == pytest.approx(22.25)


# ------------------------------------------------------------- an unfrozen (matched) song


def _matched_track(duration_s: float = 50.0):
    return SimpleNamespace(
        id="00000000-0000-0000-0000-000000000222",
        duration_s=duration_s,
        beat_timestamps_s=[0.0, 5.0, 10.0, 20.0],
        track_config={"best_start_s": 0.0, "best_end_s": duration_s},
    )


def test_a_song_shorter_than_the_ceiling_plays_whole():
    window = _window(_matched_track(50.0), 60.0, "at_most")

    assert window["duration_s"] == pytest.approx(50.0)
    assert window["validated"] is True


def test_guard_a_song_shorter_than_an_exact_length_is_still_unavailable():
    with pytest.raises(ValueError, match="no longer available"):
        _window(_matched_track(50.0), 60.0)


# ------------------------------------------------- where the render takes the bound from


def test_the_bound_comes_from_the_approved_plan_for_the_creators_length():
    plan = {"target_duration_s": 20.0, "target_duration_bound": "at_most"}

    assert gb._music_window_bound({}, plan) == "at_most"


def test_an_editor_persisted_window_duration_is_exact_even_under_a_ceiling_plan():
    plan = {"target_duration_s": 20.0, "target_duration_bound": "at_most"}

    assert gb._music_window_bound({"music_window_video_duration_s": 14.0}, plan) == "exact"


@pytest.mark.parametrize(
    "plan",
    [None, {}, {"target_duration_s": 20.0}, {"target_duration_bound": "exact"}],
)
def test_no_ceiling_in_the_plan_is_exact(plan):
    assert gb._music_window_bound({}, plan) == "exact"
