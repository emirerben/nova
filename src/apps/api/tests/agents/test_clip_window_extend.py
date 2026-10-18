"""A longer clip may start earlier in its source (KRI-737).

2026-10-18: "Increase the length of the first clip" was refused as "already at
its maximum length (0.37 seconds)" on a clip that was a short slice of a longer
source. Both the prompt and the executor measured room as ``source - in_s``, so a
clip only ever grew forward; ``patch_slots`` also silently clamped to that room.
"""

from __future__ import annotations

import math

import pytest

from app.agents.editor_ops_v2 import timeline
from app.services.kria_editor_ops import compile_editor_ops
from tests.services.test_kria_editor_ops import _job, _variant

_FPS = 30.0


def _compile(variant: dict, ops: list[dict]) -> list[dict]:
    compiled = compile_editor_ops(_job(variant), variant, ops)
    return compiled.payload.model_dump(mode="json", exclude_none=True)["timeline_slots"]


def _slot0(*, in_s: float, duration: float, source: float | None = 8.0, rate: float | None = None):
    variant = _variant()
    row = variant["ai_timeline"]["slots"][0]
    row["in_s"] = in_s
    row["duration_s"] = duration
    row["source_duration_s"] = source
    if rate is not None:
        row["playback_rate"] = rate
    return variant


def _patch(variant: dict, patch: dict, selector: dict | None = None) -> dict:
    ops = [{"op": "patch_slots", "selector": selector or {"slot_indexes": [0]}, "patch": patch}]
    return _compile(variant, ops)[0]


def _window_fits(slot: dict, source: float) -> bool:
    rate = slot.get("playback_rate") or 1.0
    return slot["in_s"] + slot["duration_s"] * rate <= source + 1e-6


def test_longer_clip_slides_its_start_earlier_when_forward_footage_runs_out() -> None:
    # The Aston Villa shape: a 0.37 s slice that ends exactly at the end of its source.
    slot = _patch(_slot0(in_s=7.63, duration=0.37), {"duration_s": 1.5})

    assert slot["duration_s"] == pytest.approx(1.5)
    assert slot["in_s"] == pytest.approx(6.5)
    assert _window_fits(slot, 8.0)


def test_request_longer_than_the_whole_source_is_capped_at_the_source() -> None:
    slot = _patch(_slot0(in_s=7.63, duration=0.37), {"duration_s": 20.0})

    assert slot["in_s"] == pytest.approx(0.0)
    assert slot["duration_s"] == pytest.approx(math.floor(8.0 * _FPS) / _FPS)
    assert _window_fits(slot, 8.0)


def test_an_explicit_start_point_is_never_slid() -> None:
    # The creator (or model) chose in_s itself: today's clamp applies, no silent move.
    slot = _patch(_slot0(in_s=7.0, duration=0.5), {"in_s": 7.0, "duration_s": 3.0})

    assert slot["in_s"] == pytest.approx(7.0)
    assert slot["duration_s"] == pytest.approx(math.floor(1.0 * _FPS) / _FPS)


def test_shorter_clip_keeps_its_start() -> None:
    slot = _patch(_slot0(in_s=5.0, duration=2.0), {"duration_s": 0.8})

    assert slot["in_s"] == pytest.approx(5.0)
    assert slot["duration_s"] == pytest.approx(0.8)


def test_clip_that_still_fits_forward_does_not_move() -> None:
    slot = _patch(_slot0(in_s=1.0, duration=2.0), {"duration_s": 4.0})

    assert slot["in_s"] == pytest.approx(1.0)
    assert slot["duration_s"] == pytest.approx(4.0)


def test_unknown_source_keeps_todays_behaviour() -> None:
    slot = _patch(_slot0(in_s=7.63, duration=0.37, source=None), {"duration_s": 1.5})

    assert slot["in_s"] == pytest.approx(7.63)
    assert slot["duration_s"] == pytest.approx(1.5)


def test_faster_playback_measures_room_in_output_seconds() -> None:
    # At 2x a 6 s window plays in 3 s of output; asking for 4 s needs 8 s of source.
    slot = _patch(_slot0(in_s=2.0, duration=3.0, rate=2.0), {"duration_s": 4.0})

    assert slot["duration_s"] == pytest.approx(4.0)
    assert slot["in_s"] == pytest.approx(0.0)
    assert _window_fits(slot, 8.0)


def test_images_are_untouched() -> None:
    variant = _variant()
    image = variant["ai_timeline"]["slots"][1]  # b.jpg, source_duration_s 3.0
    image["in_s"] = 0.0
    slot = _compile(
        variant,
        [{"op": "patch_slots", "selector": {"slot_indexes": [1]}, "patch": {"duration_s": 5.0}}],
    )[1]

    assert slot["in_s"] == pytest.approx(0.0)
    assert slot["duration_s"] == pytest.approx(5.0)


def test_set_total_duration_can_lengthen_a_clip_that_has_footage_before_its_start() -> None:
    # Clip 0 is 0.37 s at the very end of an 8 s source and clip 1 is a photo.
    # A forward-only bound pins the total at ~2.4 s; sliding the start reaches 5 s.
    variant = _slot0(in_s=7.63, duration=0.37)
    slots = _compile(
        variant,
        [{"op": "set_total_duration", "target_s": 5.0, "strategy": "uniform"}],
    )

    video = slots[0]
    assert video["duration_s"] > 0.37 + 0.5
    assert _window_fits(video, 8.0)


def test_bounds_allow_the_whole_source_not_just_the_forward_room() -> None:
    row = {"source_duration_s": 8.0, "in_s": 7.63, "duration_s": 0.37}
    _low, high = timeline._bounds(row, timeline._ops())

    assert high == pytest.approx(math.floor(8.0 * _FPS) / _FPS)
