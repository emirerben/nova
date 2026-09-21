"""KRI-7 — the zoom-in emphasis shape, and the renderers that must agree on it.

The curve lives in ONE place (`camera_effects.camera_effect_amount`); the ffmpeg
export builds its own expression from the same constants. These tests evaluate
that generated expression numerically and compare it to the model, which is what
catches the "preview looks right, export is wrong" class of bug.
"""

from __future__ import annotations

import math
import re

import pytest

from app.pipeline.camera_effects import (
    CAMERA_EFFECT_EASING_HOLD,
    CAMERA_EFFECT_EASING_PULSE,
    CAMERA_EFFECT_TOKEN,
    camera_effect_amount,
    camera_effects_from_intents,
    camera_hold_ramps,
    camera_scale_at,
    normalize_camera_effects,
    resolve_easing,
)
from app.pipeline.reframe import _build_video_filter


def effect(**overrides) -> dict:
    base = {
        "id": "camera-1",
        "token": CAMERA_EFFECT_TOKEN,
        "start_s": 2.0,
        "end_s": 5.0,
        "intensity": 0.06,
        "easing": CAMERA_EFFECT_EASING_HOLD,
        "source": "smart_captions",
    }
    base.update(overrides)
    return base


# ── the shape itself ────────────────────────────────────────────────────────


def test_zoom_in_eases_in_holds_full_intensity_then_eases_out() -> None:
    zoom = effect()
    ramp_in, ramp_out = camera_hold_ramps(3.0)

    assert camera_effect_amount(zoom, 1.99) == 0.0
    assert camera_effect_amount(zoom, 2.0) == pytest.approx(0.0, abs=1e-9)
    # Rising, not yet arrived.
    mid_ramp = camera_effect_amount(zoom, 2.0 + ramp_in / 2)
    assert 0 < mid_ramp < 0.06
    # Held flat across the body of the window — the thing a pulse cannot do.
    assert camera_effect_amount(zoom, 2.0 + ramp_in) == pytest.approx(0.06, abs=1e-9)
    assert camera_effect_amount(zoom, 3.5) == pytest.approx(0.06, abs=1e-9)
    assert camera_effect_amount(zoom, 5.0 - ramp_out) == pytest.approx(0.06, abs=1e-9)
    # Released by the end of the window.
    assert 0 < camera_effect_amount(zoom, 5.0 - ramp_out / 2) < 0.06
    assert camera_effect_amount(zoom, 5.0) == pytest.approx(0.0, abs=1e-9)
    assert camera_effect_amount(zoom, 5.01) == 0.0


def test_zoom_in_is_monotonic_and_continuous() -> None:
    """No step, no dip: a jump between frames reads as a glitch, not emphasis."""
    zoom = effect()
    samples = [camera_effect_amount(zoom, 2.0 + i / 120) for i in range(361)]
    peak = max(samples)
    peak_index = samples.index(peak)
    assert all(b - a >= -1e-9 for a, b in zip(samples[:peak_index], samples[1 : peak_index + 1]))
    assert all(b - a <= 1e-9 for a, b in zip(samples[peak_index:], samples[peak_index + 1 :]))
    # Sampled at 120Hz: ~0.2% of frame width per step, four times denser than
    # the 30fps output, so the rendered frames move even less.
    assert max(abs(b - a) for a, b in zip(samples, samples[1:])) < 0.0025


def test_pulse_shape_is_unchanged_by_the_new_easing() -> None:
    pulse = effect(easing=CAMERA_EFFECT_EASING_PULSE, start_s=3.0, end_s=4.2, intensity=0.04)
    assert camera_effect_amount(pulse, 3.6) == pytest.approx(0.04, abs=1e-9)
    assert camera_effect_amount(pulse, 3.0) == pytest.approx(0.0, abs=1e-9)
    assert camera_effect_amount(pulse, 4.2) == pytest.approx(0.0, abs=1e-9)
    # The legacy closed-form, spelled out.
    assert camera_effect_amount(pulse, 3.3) == pytest.approx(
        0.04 * math.sin(math.pi * 0.25) ** 2, abs=1e-12
    )


def test_stacked_effects_cap_total_scale() -> None:
    both = [effect(), effect(id="camera-2", intensity=0.08)]
    assert camera_scale_at(both, 3.5) == pytest.approx(1.12, abs=1e-9)


# ── normalization ───────────────────────────────────────────────────────────


def test_hold_may_run_longer_than_a_pulse() -> None:
    (long_hold,) = normalize_camera_effects([effect(start_s=0.0, end_s=20.0)])
    assert long_hold["end_s"] == 6.0
    (long_pulse,) = normalize_camera_effects(
        [effect(easing=CAMERA_EFFECT_EASING_PULSE, start_s=0.0, end_s=20.0)]
    )
    assert long_pulse["end_s"] == 2.0


def test_unknown_easing_degrades_to_the_legacy_pulse() -> None:
    assert resolve_easing("zoom_out_but_make_it_fancy") == CAMERA_EFFECT_EASING_PULSE
    (normalized,) = normalize_camera_effects([effect(easing="nonsense", end_s=9.0)])
    assert normalized["easing"] == CAMERA_EFFECT_EASING_PULSE
    assert normalized["end_s"] == 4.0  # clamped to the pulse's 2.0s ceiling


def test_normalization_clamps_to_clip_duration_and_intensity() -> None:
    (normalized,) = normalize_camera_effects([effect(intensity=5.0)], duration_s=3.5)
    assert normalized["end_s"] == 3.5
    assert normalized["intensity"] == 0.08


def test_intents_carry_their_easing_and_window() -> None:
    effects = camera_effects_from_intents(
        [
            {
                "event_id": "emphasis-2",
                "start_s": 1.0,
                "end_s": 4.0,
                "easing": CAMERA_EFFECT_EASING_HOLD,
                "intensity": 0.06,
                "role": "emphasis",
            }
        ],
        duration_s=30.0,
    )
    assert effects == [
        {
            "id": "camera-emphasis-2",
            "token": CAMERA_EFFECT_TOKEN,
            "start_s": 1.0,
            "end_s": 4.0,
            "intensity": 0.06,
            "easing": CAMERA_EFFECT_EASING_HOLD,
            "source": "smart_captions",
            "event_id": "emphasis-2",
            "effect_group_id": "emphasis-2",
            "role": "emphasis",
        }
    ]


def test_user_edited_effects_survive_a_re_plan() -> None:
    """AC: a zoom the creator moved is theirs — re-planning must not reset it."""
    mine = effect(id="mine", source="user", start_s=8.0, end_s=10.0)
    effects = camera_effects_from_intents(
        [
            {
                "event_id": "emphasis-0",
                "start_s": 1.0,
                "end_s": 3.0,
                "easing": CAMERA_EFFECT_EASING_HOLD,
            }
        ],
        existing_effects=[mine, effect(id="camera-old", source="smart_captions")],
        duration_s=30.0,
    )
    ids = [row["id"] for row in effects]
    assert "mine" in ids  # kept verbatim
    assert "camera-old" not in ids  # stale AI pick replaced
    assert "camera-emphasis-0" in ids


# ── export parity ───────────────────────────────────────────────────────────


def _ffmpeg_amount_expression(filters: list[str]) -> str:
    for segment in filters:
        match = re.search(r"scale=w='trunc\(iw\*\(1\+\((.+?)\)\)/2\)\*2'", segment)
        if match:
            return match.group(1)
    raise AssertionError("no camera scale filter was emitted")


def _evaluate(expression: str, t: float) -> float:
    """Evaluate the ffmpeg expression the way ffmpeg would, at time `t`."""
    namespace = {
        "t": t,
        "PI": math.pi,
        "sin": math.sin,
        "pow": pow,
        "min": min,
        "max": max,
        "between": lambda value, start, end: 1.0 if start <= value <= end else 0.0,
    }
    return float(eval(expression, {"__builtins__": {}}, namespace))  # noqa: S307 — our own string


@pytest.mark.parametrize("easing", [CAMERA_EFFECT_EASING_HOLD, CAMERA_EFFECT_EASING_PULSE])
def test_export_expression_matches_the_model_frame_for_frame(easing: str) -> None:
    end = 5.0 if easing == CAMERA_EFFECT_EASING_HOLD else 3.2
    zoom = effect(easing=easing, start_s=2.0, end_s=end)
    expression = _ffmpeg_amount_expression(
        _build_video_filter("9:16", None, semantic_crop_pulses=[zoom])
    )
    for frame in range(0, 210):
        t = frame / 30
        assert _evaluate(expression, t) == pytest.approx(camera_effect_amount(zoom, t), abs=1e-9)


def test_export_omits_the_scale_pass_entirely_when_nothing_is_placed() -> None:
    baseline = _build_video_filter("9:16", None)
    assert baseline == _build_video_filter("9:16", None, semantic_crop_pulses=[])
    treated = _build_video_filter("9:16", None, semantic_crop_pulses=[effect()])
    assert len(treated) == len(baseline) + 2
