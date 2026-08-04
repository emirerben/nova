"""Golden-trace tests for the Blossom Carousel spring physics port
(`app/pipeline/carousel/spring.py`). See that module's docstring for the
source repo this is an exact port of.

Section 1 pins `damp`/`project`/`release` against hand-derived reference
values (computed independently with a calculator, inlined as literals below).
Section 2 replays `CANONICAL_FLICK` through `simulate` and pins the full
frame-by-frame trace against a checked-in golden JSON fixture, plus asserts
qualitative physics properties that hold regardless of the golden fixture's
exact numbers.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.pipeline.carousel.gesture import CANONICAL_FLICK
from app.pipeline.carousel.spring import (
    FRICTION,
    SpringState,
    damp,
    is_settled,
    project,
    release,
    simulate,
)

GOLDEN_PATH = Path(__file__).parent / "golden_spring_trace.json"


# --- Section 1: hand-derived reference values ------------------------------


def test_damp_single_reference_frame():
    # One reference frame (delta_ms == 1000/60) collapses the exponent to
    # exactly `t`, so damp(0, 100, 0.12, 1000/60) == 0 + 100 * 0.12 == 12.0.
    assert damp(0.0, 100.0, 0.12, 1000 / 60) == pytest.approx(12.0)


def test_damp_two_reference_frames():
    # Over two reference-frames-worth of delta, the smoothing factor is
    # 1 - (1 - t)**2, i.e. damp == 100 * (1 - (1 - 0.12)**2) == 22.56.
    expected = 100 * (1 - (1 - 0.12) ** 2)
    assert expected == pytest.approx(22.56)
    assert damp(0.0, 100.0, 0.12, 2 * 1000 / 60) == pytest.approx(expected)


def test_project_reference_value():
    assert project(0.0, 28.0, 0.72) == pytest.approx(100.0)


def test_release_reference_case():
    # state(target=0, velocity=50, total_drag_px=400 — a real flick, well past
    # DRAG_ACTIVATION_PX=10, so release()'s snap-seeking logic actually runs;
    # see spring.py's module docstring point 2), snap_positions=[0, 588, 1176],
    # snapport=1080.
    #   velocity * 2        = 100
    #   resting_x           = project(0, 100, 0.72) = 0 + 100 / 0.28 ≈ 357.142857
    #   threshold           = 1080 / 3 = 360
    #   candidates within threshold of resting_x: 0 (|0-357.14|=357.14<=360)
    #                                              588 (|588-357.14|=230.86<=360)
    #   nearest candidate to resting_x -> 588
    #   force = (588 - 0) * (1 - 0.72) * (1 / 0.72) = 588 * 0.28 / 0.72 ≈ 228.6667
    state = SpringState(
        target=0.0, velocity=50.0, virtual_scroll=0.0, is_dragging=True, total_drag_px=400.0
    )
    resting_x = project(0.0, 100.0, FRICTION)
    assert resting_x == pytest.approx(357.142857, abs=1e-6)

    result = release(state, snap_positions=[0.0, 588.0, 1176.0], snapport_width=1080.0)

    assert result.is_dragging is False
    assert result.velocity == pytest.approx(588 * 0.28 / 0.72, abs=1e-6)
    assert result.velocity == pytest.approx(228.666667, abs=1e-6)
    # target/virtual_scroll are untouched by release().
    assert result.target == pytest.approx(0.0)
    assert result.virtual_scroll == pytest.approx(0.0)


def test_release_below_drag_activation_threshold_is_a_no_op_tap():
    # Bundle: `_.x <= 10` (P()'s bail-out) skips the whole snap-seeking dance
    # for a sub-DRAG_ACTIVATION_PX gesture — just stops dragging as-is.
    state = SpringState(
        target=12.0, velocity=3.0, virtual_scroll=5.0, is_dragging=True, total_drag_px=4.0
    )

    result = release(state, snap_positions=[0.0, 588.0, 1176.0], snapport_width=1080.0)

    assert result.is_dragging is False
    assert result.velocity == pytest.approx(3.0)
    assert result.target == pytest.approx(12.0)
    assert result.virtual_scroll == pytest.approx(5.0)


def test_release_clamps_chosen_snap_to_bounds():
    # Bundle's ne(): the resolved snap target is clamped to
    # [min(0,(scrollWidth-scrollerWidth)*dir), max(...)] before becoming a
    # release force. nearest snap to a huge resting_x is 1176, but bounds cap
    # it at 900 — force must be computed from the CLAMPED value.
    state = SpringState(
        target=0.0, velocity=1000.0, virtual_scroll=0.0, is_dragging=True, total_drag_px=400.0
    )

    result = release(
        state,
        snap_positions=[0.0, 588.0, 1176.0],
        snapport_width=1080.0,
        bounds=(0.0, 900.0),
    )

    expected_force = (900.0 - 0.0) * (1 - FRICTION) * (1 / FRICTION)
    assert result.velocity == pytest.approx(expected_force, abs=1e-6)


def test_is_settled_rounds_to_twelve_places():
    assert is_settled(SpringState(velocity=0.0))
    assert is_settled(SpringState(velocity=4.4e-13))
    assert not is_settled(SpringState(velocity=1e-9))


# --- Section 2: golden trace -------------------------------------------------

SNAP_POSITIONS = [i * 588 for i in range(5)]
SNAPPORT_WIDTH = 1080.0


def _run_canonical_trace():
    return simulate(
        CANONICAL_FLICK,
        snap_positions=SNAP_POSITIONS,
        snapport_width=SNAPPORT_WIDTH,
    )


def test_canonical_flick_matches_golden_trace():
    frames = _run_canonical_trace()
    golden = json.loads(GOLDEN_PATH.read_text())

    assert len(frames) == len(golden)
    for frame, expected in zip(frames, golden, strict=True):
        assert frame.t_s == pytest.approx(expected["t_s"], abs=1e-6)
        assert frame.virtual_scroll == pytest.approx(expected["virtual_scroll"], abs=1e-6)
        assert frame.velocity == pytest.approx(expected["velocity"], abs=1e-6)
        assert frame.target == pytest.approx(expected["target"], abs=1e-6)


def test_canonical_flick_settles_near_a_snap_position():
    frames = _run_canonical_trace()
    final = frames[-1]

    # Sanity print (see module docstring note below): the canonical flick
    # lands on snap index 2 (1176px = 2 * 588), i.e. it advances two cards.
    nearest_index = min(
        range(len(SNAP_POSITIONS)), key=lambda i: abs(SNAP_POSITIONS[i] - final.virtual_scroll)
    )
    assert nearest_index == 2
    assert abs(SNAP_POSITIONS[nearest_index] - final.virtual_scroll) < 1.0


def test_canonical_flick_trace_length_is_bounded():
    frames = _run_canonical_trace()
    assert len(frames) < 200


def test_canonical_flick_converges_monotonically_without_overshoot():
    """Qualitative-physics check, independent of the golden fixture's exact
    numbers.

    NOTE ON THE MODEL: `release()` computes `force` directly from the
    distance to the *chosen* `slide_x` (not from the raw `resting_x`
    momentum-projection used only to pick the candidate). Algebraically,
    chaining `tick`'s post-release recurrence gives
    `target_n = slide_x - (slide_x - target_0) * FRICTION**n`, which is
    monotonic and never crosses `slide_x` for any n; `virtual_scroll` is in
    turn a `damp` (weighted average) of a monotonically-bounded target
    sequence, so it can't exceed `slide_x` either. This is a critically
    damped double-exponential chase, not an underdamped spring/oscillator —
    it decelerates into the snap point rather than overshooting past it and
    settling back. So `max(virtual_scroll) == final virtual_scroll` here by
    construction (both attained on the last frame), rather than the
    overshoot-then-settle-back shape a mass-spring-damper would produce.
    """
    frames = _run_canonical_trace()
    scrolls = [f.virtual_scroll for f in frames]

    assert max(scrolls) == pytest.approx(scrolls[-1], abs=1e-6)
    # Monotonically non-decreasing throughout (converging from below).
    assert all(b >= a - 1e-9 for a, b in zip(scrolls[:-1], scrolls[1:], strict=True))


def test_release_force_recurrence_proof_holds_numerically():
    """Independent numeric check of the closed-form recurrence used in the
    docstring above: target_n = slide_x - (slide_x - target_0) * FRICTION**n.
    """
    target_0 = 400.0
    slide_x = 1176.0
    force = (slide_x - target_0) * (1 - FRICTION) * (1 / FRICTION)

    state = SpringState(target=target_0, velocity=force, virtual_scroll=0.0, is_dragging=False)
    for n in range(1, 21):
        velocity = state.velocity * FRICTION
        target = state.target + velocity
        state = SpringState(target=target, velocity=velocity, virtual_scroll=0.0, is_dragging=False)
        expected = slide_x - (slide_x - target_0) * FRICTION**n
        assert target == pytest.approx(expected, abs=1e-6)
        assert target < slide_x + 1e-9
