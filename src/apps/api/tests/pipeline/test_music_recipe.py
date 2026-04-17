"""Unit tests for app/pipeline/music_recipe.py.

No I/O, no DB — pure algorithm tests.
"""

import pytest

from app.pipeline.music_recipe import auto_best_section, generate_music_recipe


# ── auto_best_section ─────────────────────────────────────────────────────────


def test_auto_best_section_empty_beats() -> None:
    start, end = auto_best_section([], window_s=30.0, track_duration_s=120.0)
    assert start == 0.0
    assert end == pytest.approx(30.0)


def test_auto_best_section_empty_beats_no_duration() -> None:
    start, end = auto_best_section([], window_s=45.0)
    assert start == 0.0
    assert end == pytest.approx(45.0)


def test_auto_best_section_finds_peak_density() -> None:
    # Sparse beats 0-30s, dense beats 60-90s
    sparse = [float(i) for i in range(0, 30, 4)]       # 8 beats
    dense = [float(i) for i in range(60, 90, 1)]        # 30 beats
    all_beats = sparse + dense

    start, end = auto_best_section(all_beats, window_s=30.0, track_duration_s=120.0)

    # Dense cluster should win
    assert start >= 55.0, f"Expected start near 60s, got {start}"
    assert end == pytest.approx(start + 30.0, abs=1.0)


def test_auto_best_section_caps_to_track_duration() -> None:
    beats = [10.0, 20.0, 25.0, 28.0]  # cluster near end
    start, end = auto_best_section(beats, window_s=30.0, track_duration_s=35.0)
    assert end <= 35.0
    assert start >= 0.0


def test_auto_best_section_track_shorter_than_window() -> None:
    beats = [1.0, 3.0, 5.0]
    start, end = auto_best_section(beats, window_s=30.0, track_duration_s=10.0)
    assert end <= 10.0
    assert start >= 0.0


# ── generate_music_recipe ─────────────────────────────────────────────────────


def _make_track_data(
    beats: list[float],
    best_start: float = 0.0,
    best_end: float = 30.0,
    slot_every_n: int = 8,
    duration_s: float = 60.0,
) -> dict:
    return {
        "beat_timestamps_s": beats,
        "track_config": {
            "best_start_s": best_start,
            "best_end_s": best_end,
            "slot_every_n_beats": slot_every_n,
        },
        "duration_s": duration_s,
    }


def test_generate_music_recipe_basic() -> None:
    # 17 beats at 0.5s intervals = 8.5s range, every 4 beats → 2 slots
    beats = [i * 0.5 for i in range(17)]
    data = _make_track_data(beats, best_start=0.0, best_end=8.5, slot_every_n=4)
    recipe = generate_music_recipe(data)

    assert recipe["shot_count"] >= 2
    assert len(recipe["slots"]) == recipe["shot_count"]
    for slot in recipe["slots"]:
        assert slot["target_duration_s"] > 0
        assert slot["slot_type"] == "broll"


def test_generate_music_recipe_empty_beats() -> None:
    data = _make_track_data([], best_start=0.0, best_end=30.0)
    with pytest.raises(ValueError, match="0 slots"):
        generate_music_recipe(data)


def test_generate_music_recipe_window_boundaries_respected() -> None:
    # Beats from 0–60s; window is 30–50s
    beats = [float(i) for i in range(0, 60)]
    data = _make_track_data(beats, best_start=30.0, best_end=50.0, slot_every_n=4)
    recipe = generate_music_recipe(data)

    # All beat timestamps in recipe should be relative to start (≥0)
    for b in recipe["beat_timestamps_s"]:
        assert b >= 0.0, f"Beat {b} is before start"
    # Total duration ≈ window length
    assert recipe["total_duration_s"] == pytest.approx(20.0, abs=1.0)


def test_generate_music_recipe_slot_positions_sequential() -> None:
    beats = [float(i) for i in range(0, 40)]
    data = _make_track_data(beats, best_start=0.0, best_end=39.0, slot_every_n=8)
    recipe = generate_music_recipe(data)

    positions = [s["position"] for s in recipe["slots"]]
    assert positions == list(range(1, len(positions) + 1))


def test_generate_music_recipe_required_clips_derived() -> None:
    beats = [float(i) for i in range(0, 40)]
    data = _make_track_data(beats, best_start=0.0, best_end=39.0, slot_every_n=4)
    recipe = generate_music_recipe(data)

    n_slots = recipe["shot_count"]
    assert recipe["required_clips_min"] >= 1
    assert recipe["required_clips_max"] >= recipe["required_clips_min"]
    assert recipe["required_clips_max"] <= n_slots


def test_generate_music_recipe_too_few_beats_for_slots() -> None:
    # Only 3 beats, slot_every_n=8 → impossible → 0 slots
    beats = [1.0, 2.0, 3.0]
    data = _make_track_data(beats, best_start=0.0, best_end=10.0, slot_every_n=8)
    with pytest.raises(ValueError, match="0 slots"):
        generate_music_recipe(data)


def test_generate_music_recipe_sync_style() -> None:
    beats = [float(i) for i in range(0, 40)]
    data = _make_track_data(beats, best_start=0.0, best_end=39.0, slot_every_n=4)
    recipe = generate_music_recipe(data)
    assert recipe["sync_style"] == "cut-on-beat"
