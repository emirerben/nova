"""Tests for `app.pipeline.carousel.choreography` — FOCUS CHOREOGRAPHY
(`build_timeline`) and ROLLING (`rolling_timeline`) timeline authoring.

Pure Python/math — no skia, no ffmpeg. Every property asserted here is a
property of the AUTHORED `FrameState` sequence, independent of how the
renderer later paints it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.pipeline.carousel.choreography import (
    DIM_MAX,
    FocusMoment,
    build_timeline,
    rolling_timeline,
)
from app.pipeline.carousel.effects import CardGeometry, snap_bounds

FPS = 30
GEO = CardGeometry(card_w=540, card_h=720, gap=48, corner_radius=24)
PITCH = GEO.card_w + GEO.gap  # 588
VIEWPORT_W = 1080.0


def _snap(index: int) -> float:
    return index * PITCH


def _assert_t_s_strictly_increasing_by_dt(frames, fps=FPS):
    dt = 1.0 / fps
    for a, b in zip(frames, frames[1:]):
        assert b.t_s - a.t_s == pytest.approx(dt, abs=1e-9)


def _assert_scroll_continuous(frames, *, max_jump):
    for a, b in zip(frames, frames[1:]):
        assert abs(b.scroll_x - a.scroll_x) <= max_jump


def _assert_focus_t_continuous_within_runs(frames, *, max_delta):
    for a, b in zip(frames, frames[1:]):
        if a.focus_card is not None and a.focus_card == b.focus_card:
            assert abs(b.focus_t - a.focus_t) <= max_delta


# --------------------------------------------------------------------------- #
# build_timeline
# --------------------------------------------------------------------------- #


def test_build_timeline_t_s_strictly_increasing():
    frames = build_timeline(4, GEO, VIEWPORT_W, focus_moments=(FocusMoment(card_index=1),), seed=0)
    assert len(frames) > 0
    _assert_t_s_strictly_increasing_by_dt(frames)


def test_build_timeline_scroll_continuous_no_large_jumps():
    frames = build_timeline(4, GEO, VIEWPORT_W, focus_moments=(FocusMoment(card_index=1),), seed=0)
    _assert_scroll_continuous(frames, max_jump=PITCH / 2)


def test_build_timeline_focus_t_continuous_within_a_focus_run():
    frames = build_timeline(4, GEO, VIEWPORT_W, focus_moments=(FocusMoment(card_index=1),), seed=0)
    # Default zoom_s=0.6 -> 18-frame ramp; ease-out-cubic's steepest step
    # (frame 1) is ~0.167 there — comfortably under a 0.2/frame budget.
    _assert_focus_t_continuous_within_runs(frames, max_delta=0.2)


def test_build_timeline_focus_reaches_exactly_one_and_returns_to_zero():
    frames = build_timeline(4, GEO, VIEWPORT_W, focus_moments=(FocusMoment(card_index=2),), seed=0)
    focus_frames = [f for f in frames if f.focus_card == 2]
    assert focus_frames, "expected at least one frame focused on card 2"
    assert max(f.focus_t for f in focus_frames) == pytest.approx(1.0)
    assert focus_frames[-1].focus_t == pytest.approx(0.0)
    assert focus_frames[0].focus_t > 0.0  # the run only exists while focus_t > 0


def test_build_timeline_dim_bounded_by_dim_max():
    frames = build_timeline(4, GEO, VIEWPORT_W, focus_moments=(FocusMoment(card_index=1),), seed=0)
    for f in frames:
        assert 0.0 <= f.dim <= DIM_MAX + 1e-9


def test_build_timeline_dim_zero_when_not_focused():
    frames = build_timeline(4, GEO, VIEWPORT_W, focus_moments=(), seed=0)
    assert frames  # lead-in hold alone still produces frames
    assert all(f.focus_card is None and f.dim == 0.0 for f in frames)


def test_build_timeline_seed_determinism_same_seed_identical():
    frames_a = build_timeline(
        4, GEO, VIEWPORT_W, focus_moments=(FocusMoment(card_index=1),), seed=7
    )
    frames_b = build_timeline(
        4, GEO, VIEWPORT_W, focus_moments=(FocusMoment(card_index=1),), seed=7
    )
    assert frames_a == frames_b


def test_build_timeline_different_seed_differs():
    frames_a = build_timeline(
        4, GEO, VIEWPORT_W, focus_moments=(FocusMoment(card_index=1),), seed=0
    )
    frames_b = build_timeline(
        4, GEO, VIEWPORT_W, focus_moments=(FocusMoment(card_index=1),), seed=1
    )
    assert frames_a != frames_b


@pytest.mark.parametrize("target_index", [0, 1, 2, 3, 4])
def test_build_timeline_flick_lands_within_one_px_of_requested_card(target_index):
    """Every requested focus card must actually get centered (within 1px of
    its flat snap position) at some point in the timeline — including
    card_index=0 (already centered at the lead-in position, no flick) and
    the highest index (a multi-card forward flick)."""
    n_cards = 5
    frames = build_timeline(
        n_cards,
        GEO,
        VIEWPORT_W,
        focus_moments=(FocusMoment(card_index=target_index, hold_s=0.2, zoom_s=0.2),),
        seed=3,
    )
    focus_frames = [f for f in frames if f.focus_card == target_index]
    assert focus_frames, f"card {target_index} never became the focus target"
    scrolls = {round(f.scroll_x, 3) for f in focus_frames}
    assert len(scrolls) == 1, (
        f"expected a single centered scroll position while focused, got {scrolls}"
    )
    (scroll_x,) = scrolls
    assert abs(scroll_x - _snap(target_index)) < 1.0


def test_build_timeline_backward_flick_lands_correctly():
    """Two moments in descending visual order (3 then 1) — `build_timeline`
    sorts by card_index internally, but the flick TO card 1 (after having
    just centered on card 3) is a genuine backward flick; confirm it still
    lands exactly."""
    frames = build_timeline(
        6,
        GEO,
        VIEWPORT_W,
        focus_moments=(
            FocusMoment(card_index=1, hold_s=0.2, zoom_s=0.2),
            FocusMoment(card_index=4, hold_s=0.2, zoom_s=0.2),
        ),
        seed=5,
    )
    for target_index in (1, 4):
        focus_frames = [f for f in frames if f.focus_card == target_index]
        assert focus_frames
        scrolls = {round(f.scroll_x, 3) for f in focus_frames}
        assert len(scrolls) == 1
        (scroll_x,) = scrolls
        assert abs(scroll_x - _snap(target_index)) < 1.0


def test_build_timeline_moments_processed_in_ascending_card_index_order():
    """Regardless of input order, focus events must occur in ascending
    card_index order (per the design brief: "For each focus moment, in
    card_index order")."""
    frames = build_timeline(
        6,
        GEO,
        VIEWPORT_W,
        focus_moments=(
            FocusMoment(card_index=4, hold_s=0.2, zoom_s=0.2),
            FocusMoment(card_index=1, hold_s=0.2, zoom_s=0.2),
        ),
        seed=2,
    )
    seen_order = []
    for f in frames:
        if f.focus_card is not None and (not seen_order or seen_order[-1] != f.focus_card):
            seen_order.append(f.focus_card)
    assert seen_order == [1, 4]


def test_manual_timing_preserves_sequence_order_and_exact_phase_frames():
    fixture = json.loads(
        (
            Path(__file__).resolve().parents[6]
            / "tests"
            / "fixtures"
            / "carousel-timing"
            / "manual-v1.json"
        ).read_text()
    )
    frames = build_timeline(
        fixture["n_cards"],
        GEO,
        VIEWPORT_W,
        focus_moments=tuple(
            FocusMoment(
                card_index=item["clip_index"],
                hold_s=item["hold_s"],
                zoom_s=fixture["zoom_duration_s"],
            )
            for item in fixture["sequence"]
        ),
        lead_in_s=0,
        settle_pad_s=0,
        manual_timing=True,
        move_duration_s=fixture["move_duration_s"],
        seed=99,
    )
    seen_order = []
    for frame in frames:
        if frame.focus_card is not None and (not seen_order or seen_order[-1] != frame.focus_card):
            seen_order.append(frame.focus_card)
    assert seen_order == fixture["expected_focus_order"]
    # Subtract the zoom-in ramp's final frame, which is exactly focus_t=1.
    for card_index, expected_frames in zip(
        fixture["expected_focus_order"], fixture["expected_hold_frames"]
    ):
        assert (
            sum(f.focus_card == card_index and f.focus_t == 1.0 for f in frames) - 1
            == expected_frames
        )
    assert sum(frame.focus_card is None for frame in frames) == (
        fixture["expected_move_frames"] * len(fixture["sequence"])
    )
    # Same manual inputs ignore seed because timing jitter is disabled.
    again = build_timeline(
        5,
        GEO,
        VIEWPORT_W,
        focus_moments=(
            FocusMoment(card_index=3, hold_s=0.5, zoom_s=0.2),
            FocusMoment(card_index=1, hold_s=0.7, zoom_s=0.2),
        ),
        lead_in_s=0,
        settle_pad_s=0,
        manual_timing=True,
        move_duration_s=fixture["move_duration_s"],
        seed=1,
    )
    assert frames == again


def test_manual_rolling_preserves_sequence_order_and_exact_holds():
    fixture = json.loads(
        (
            Path(__file__).resolve().parents[6]
            / "tests"
            / "fixtures"
            / "carousel-timing"
            / "manual-v1.json"
        ).read_text()
    )
    frames = rolling_timeline(
        fixture["n_cards"],
        GEO,
        VIEWPORT_W,
        duration_s=fixture["rolling_duration_s"],
        sequence=tuple(
            FocusMoment(card_index=item["clip_index"], hold_s=item["hold_s"])
            for item in fixture["sequence"]
        ),
        move_duration_s=fixture["move_duration_s"],
        manual_timing=True,
        seed=99,
    )
    runs: list[tuple[int, int]] = []
    for item, expected_hold in zip(fixture["sequence"], fixture["expected_hold_frames"]):
        snap = _snap(item["clip_index"])
        longest = 0
        current = 0
        for frame in frames:
            if frame.scroll_x == pytest.approx(snap):
                current += 1
                longest = max(longest, current)
            else:
                current = 0
        runs.append((item["clip_index"], longest))
        assert longest >= expected_hold
    assert [clip_index for clip_index, _ in runs] == fixture["expected_focus_order"]
    observed_order: list[int] = []
    for index, frame in enumerate(frames):
        for clip_index in fixture["expected_focus_order"]:
            snap = _snap(clip_index)
            previous_at_same_snap = index > 0 and frames[index - 1].scroll_x == pytest.approx(snap)
            if frame.scroll_x == pytest.approx(snap) and not previous_at_same_snap:
                observed_order.append(clip_index)
    assert observed_order == fixture["expected_focus_order"]
    assert (
        len(frames)
        == sum(fixture["expected_hold_frames"])
        + len(fixture["expected_focus_order"]) * fixture["expected_move_frames"]
    )


def test_build_timeline_ends_in_motion_rest_not_frozen_on_focus():
    frames = build_timeline(
        4,
        GEO,
        VIEWPORT_W,
        focus_moments=(FocusMoment(card_index=1, hold_s=0.2, zoom_s=0.2),),
        seed=0,
    )
    assert frames[-1].focus_card is None
    assert frames[-1].dim == 0.0


def test_build_timeline_no_focus_moments_still_returns_lead_in():
    frames = build_timeline(3, GEO, VIEWPORT_W, focus_moments=(), seed=0)
    assert len(frames) > 0
    assert all(f.scroll_x == pytest.approx(frames[0].scroll_x) for f in frames)


def test_build_timeline_respects_snap_bounds():
    """The final trailing flick (or any flick) never sends `scroll_x` outside
    the scroller's legal flat range."""
    n_cards = 4
    frames = build_timeline(
        n_cards,
        GEO,
        VIEWPORT_W,
        focus_moments=(FocusMoment(card_index=3, hold_s=0.1, zoom_s=0.1),),
        seed=0,
    )
    lo, hi = snap_bounds(n_cards, GEO, VIEWPORT_W)
    for f in frames:
        assert lo - 1e-6 <= f.scroll_x <= hi + 1e-6


# --------------------------------------------------------------------------- #
# rolling_timeline
# --------------------------------------------------------------------------- #


def test_rolling_timeline_frame_count_matches_duration():
    duration_s = 2.5
    frames = rolling_timeline(5, GEO, VIEWPORT_W, duration_s=duration_s, seed=0)
    assert len(frames) == round(duration_s * FPS)


def test_rolling_timeline_t_s_strictly_increasing():
    frames = rolling_timeline(4, GEO, VIEWPORT_W, duration_s=2.0, seed=0)
    _assert_t_s_strictly_increasing_by_dt(frames)


def test_rolling_timeline_scroll_continuous_no_large_jumps():
    frames = rolling_timeline(4, GEO, VIEWPORT_W, duration_s=2.0, seed=0)
    _assert_scroll_continuous(frames, max_jump=PITCH / 2)


def test_rolling_timeline_no_focus_ever():
    frames = rolling_timeline(4, GEO, VIEWPORT_W, duration_s=2.0, seed=0)
    assert all(f.focus_card is None and f.focus_t == 0.0 and f.dim == 0.0 for f in frames)


def test_rolling_timeline_advances_through_multiple_cards():
    """A duration generous enough to cover several cards must actually visit
    scroll positions near more than one card's snap."""
    n_cards = 4
    frames = rolling_timeline(n_cards, GEO, VIEWPORT_W, duration_s=4.0, seed=0)
    snaps_hit = {i for i in range(n_cards) if any(abs(f.scroll_x - _snap(i)) < 1.0 for f in frames)}
    assert len(snaps_hit) >= 2


def test_rolling_timeline_seed_determinism():
    frames_a = rolling_timeline(4, GEO, VIEWPORT_W, duration_s=2.0, seed=9)
    frames_b = rolling_timeline(4, GEO, VIEWPORT_W, duration_s=2.0, seed=9)
    assert frames_a == frames_b


def test_rolling_timeline_different_seed_differs():
    frames_a = rolling_timeline(4, GEO, VIEWPORT_W, duration_s=2.0, seed=0)
    frames_b = rolling_timeline(4, GEO, VIEWPORT_W, duration_s=2.0, seed=1)
    assert frames_a != frames_b


def test_rolling_timeline_pads_short_duration_by_holding_last_position():
    """A `duration_s` too short to even complete the lead-in hold must still
    return exactly `round(duration_s * fps)` frames (padding, not crashing)."""
    frames = rolling_timeline(5, GEO, VIEWPORT_W, duration_s=0.1, seed=0)
    assert len(frames) == round(0.1 * FPS)
