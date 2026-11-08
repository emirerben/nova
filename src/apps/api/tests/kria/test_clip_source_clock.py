import pytest

from app.kria.clip_source_clock import (
    resize_source_window,
    retime_source_window,
    slice_source_window,
)


@pytest.fixture
def row():
    return {
        "slot_id": "a",
        "in_s": 2,
        "source_end_s": 4,
        "playback_rate": 1,
        "duration_s": 4,
        "freeze_hold_s": 2,
        "freeze_frame_s": 4,
        "source_duration_s": 10,
        "muted": True,
    }


def test_slice_movement_and_hold_preserves_frame(row):
    part = slice_source_window(row, 1, 3)
    assert (part["in_s"], part["source_end_s"], part["duration_s"], part["freeze_hold_s"]) == (
        3,
        4,
        2,
        1,
    )
    assert part["freeze_frame_s"] == 4
    assert part["muted"] is True
    assert row["in_s"] == 2


def test_slice_entirely_inside_hold_samples_same_frame(row):
    part = slice_source_window(row, 3, 4)
    assert part["in_s"] == pytest.approx(4 - 1 / 30)
    assert part["source_end_s"] == part["freeze_frame_s"] == 4
    assert part["freeze_hold_s"] == pytest.approx(1 - 1 / 30)
    assert part["duration_s"] == 1
    assert part["playback_rate"] == 1


def test_speed_changes_movement_and_preserves_hold(row):
    part = retime_source_window(row, 2)
    assert part["duration_s"] == 3
    assert part["freeze_hold_s"] == 2
    assert part["source_end_s"] == part["freeze_frame_s"] == 4
    part = resize_source_window(part, 2)
    assert part["freeze_hold_s"] == 1
    assert part["duration_s"] == 2
