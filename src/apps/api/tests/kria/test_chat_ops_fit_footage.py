"""A chat edit never stages a clip window longer than its footage (KRI-772).

Troya Road Trip, 2026-10-21: chat was asked to make the first wooden-horse shot 3 seconds. The
clip is 3.36 s long and plays from 0.9 s, so 3.0 s needs 3.9 s of footage. `set_clip_duration`
wrote 3.0 anyway; the editor opened that draft; Save refused it with `TIMELINE_OUT_OF_BOUNDS`
(overshoot 0.54 s) and the app said only "This save was rejected".

The producer owns the invariant: whatever chat stages must be a window the Save accepts.
"""

from __future__ import annotations

import pytest

from app.routes.generative_jobs import resolve_timeline_slots_for_edit
from app.services.kria_editor_ops import compile_editor_ops, resolve_editor_base
from tests.kria.test_editor_state_turns import _state
from tests.routes.test_generative_timeline import _arm, _timeline_job

SOURCE_S = 3.36


def _row(index: int = 0, *, in_s: float = 0.9, duration_s: float = 0.666667, **extra) -> dict:
    return {
        "slot_id": f"s{index + 1}",
        "clip_index": index,
        "in_s": in_s,
        "duration_s": duration_s,
        "source_duration_s": SOURCE_S,
        **extra,
    }


def _compile(monkeypatch, rows: list[dict], ops: list[dict]):
    _arm(monkeypatch)
    job = _timeline_job(beat_grid=[])
    variant = job.assembly_plan["variants"][0]
    variant["render_generation_id"] = "G2"
    base = resolve_editor_base(job, variant, None, _state({"timeline_slots": rows}))
    return job, variant, compile_editor_ops(job, base.projected, ops)


def _slots(compiled) -> list[dict]:
    return [slot.model_dump() for slot in compiled.payload.timeline_slots]


def test_troya_three_seconds_on_a_3_36_second_clip_is_fitted_to_the_footage(monkeypatch) -> None:
    job, variant, compiled = _compile(
        monkeypatch,
        [_row(0), _row(1, in_s=0.0, duration_s=1.0)],
        [{"op": "set_clip_duration", "slot_index": 0, "duration_s": 3.0}],
    )
    first = _slots(compiled)[0]
    assert first["in_s"] == pytest.approx(0.9)
    # 3.36 - 0.9 = 2.46 s of footage is all there is; the draft says so instead of overrunning.
    assert first["duration_s"] == pytest.approx(2.46, abs=1e-5)
    assert first["in_s"] + first["duration_s"] <= SOURCE_S + 1e-9
    assert any("2.46" in change for change in compiled.changes), compiled.changes
    # And the window is one the real Save accepts.
    resolve_timeline_slots_for_edit(job, variant, compiled.payload.timeline_slots)


def test_a_window_inside_the_footage_is_untouched(monkeypatch) -> None:
    _job, _variant, compiled = _compile(
        monkeypatch,
        [_row(0)],
        [{"op": "set_clip_duration", "slot_index": 0, "duration_s": 2.0}],
    )
    assert _slots(compiled)[0]["duration_s"] == pytest.approx(2.0)
    assert not any("footage" in change for change in compiled.changes)


def test_a_start_past_the_end_slides_back_instead_of_leaving_nothing(monkeypatch) -> None:
    _job, _variant, compiled = _compile(
        monkeypatch,
        [_row(0, in_s=0.0, duration_s=1.0)],
        [{"op": "set_clip_in", "slot_index": 0, "in_s": 3.3}],
    )
    slot = _slots(compiled)[0]
    assert slot["in_s"] + slot["duration_s"] <= SOURCE_S + 1e-9
    assert slot["duration_s"] >= 0.1
    assert slot["in_s"] >= 0


def test_playback_rate_is_part_of_the_footage_a_window_needs(monkeypatch) -> None:
    _job, _variant, compiled = _compile(
        monkeypatch,
        [_row(0, in_s=1.0, duration_s=0.5, playback_rate=2.0)],
        [{"op": "set_clip_duration", "slot_index": 0, "duration_s": 3.0}],
    )
    slot = _slots(compiled)[0]
    # 2.36 s of footage at 2x lasts 1.18 s.
    assert slot["in_s"] + slot["duration_s"] * 2.0 <= SOURCE_S + 1e-9
    assert slot["duration_s"] == pytest.approx(1.18, abs=1e-5)


def test_a_clip_with_no_known_footage_length_is_left_alone(monkeypatch) -> None:
    row = _row(0)
    row.pop("source_duration_s")
    _job, _variant, compiled = _compile(
        monkeypatch, [row], [{"op": "set_clip_duration", "slot_index": 0, "duration_s": 9.0}]
    )
    assert _slots(compiled)[0]["duration_s"] == pytest.approx(9.0)
