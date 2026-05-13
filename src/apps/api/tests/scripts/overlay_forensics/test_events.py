"""Tests for scripts/overlay_forensics/events.py."""
from __future__ import annotations

from scripts.overlay_forensics.events import (
    FrameObservation,
    TextEvent,
    classify_entrance,
    cluster_observations_to_events,
    cooccurrence_intervals,
)
from scripts.overlay_forensics.masking import BBox


def _obs(t: float, color: str, bbox: BBox, pixels: int = 1000,
         brightness: float = 0.8) -> FrameObservation:
    return FrameObservation(
        t_s=t, color_key=color, bbox=bbox,
        mask_pixel_count=pixels, mean_brightness=brightness,
    )


WHITE_LEFT = BBox(x_min=100, y_min=200, x_max=199, y_max=249)
WHITE_RIGHT = BBox(x_min=700, y_min=200, x_max=799, y_max=249)
MAIZE_CENTER = BBox(x_min=400, y_min=600, x_max=599, y_max=799)


def test_cluster_contiguous_observations_same_region_same_color():
    obs = [
        _obs(0.00, "white", WHITE_LEFT),
        _obs(0.05, "white", WHITE_LEFT),
        _obs(0.10, "white", WHITE_LEFT),
        _obs(0.15, "white", WHITE_LEFT),
    ]
    events = cluster_observations_to_events(obs, 1080, 1920, step_s=0.05)
    assert len(events) == 1
    assert events[0].color_key == "white"
    assert events[0].duration_s == 0.15


def test_cluster_tolerates_1_frame_gap():
    obs = [
        _obs(0.00, "white", WHITE_LEFT),
        _obs(0.05, "white", WHITE_LEFT),
        # gap at 0.10
        _obs(0.15, "white", WHITE_LEFT),
        _obs(0.20, "white", WHITE_LEFT),
    ]
    events = cluster_observations_to_events(
        obs, 1080, 1920, step_s=0.05, gap_tolerance_steps=1,
    )
    assert len(events) == 1


def test_cluster_splits_on_large_gap():
    obs = [
        _obs(0.00, "white", WHITE_LEFT),
        _obs(0.05, "white", WHITE_LEFT),
        # 4-frame gap (0.10..0.25 missing)
        _obs(0.30, "white", WHITE_LEFT),
        _obs(0.35, "white", WHITE_LEFT),
    ]
    events = cluster_observations_to_events(
        obs, 1080, 1920, step_s=0.05, gap_tolerance_steps=1,
    )
    assert len(events) == 2


def test_cluster_separates_by_color_and_region():
    obs = [
        _obs(0.00, "white", WHITE_LEFT),
        _obs(0.00, "white", WHITE_RIGHT),
        _obs(0.00, "maize", MAIZE_CENTER),
        _obs(0.05, "white", WHITE_LEFT),
        _obs(0.05, "white", WHITE_RIGHT),
        _obs(0.05, "maize", MAIZE_CENTER),
        _obs(0.10, "white", WHITE_LEFT),
        _obs(0.10, "white", WHITE_RIGHT),
        _obs(0.10, "maize", MAIZE_CENTER),
    ]
    events = cluster_observations_to_events(obs, 1080, 1920, step_s=0.05)
    assert len(events) == 3
    colors = sorted(e.color_key for e in events)
    assert colors == ["maize", "white", "white"]


def test_cluster_drops_events_below_min_duration():
    obs = [
        _obs(0.00, "white", WHITE_LEFT),
        # Single-frame "event" — below min_duration=0.10 since step is 0.05.
    ]
    events = cluster_observations_to_events(
        obs, 1080, 1920, step_s=0.05, min_duration_s=0.10,
    )
    assert len(events) == 0


def test_classify_entrance_slide_up_detected():
    # Y centroid drops from 1500 → 600 (well over 5% of 1920px frame).
    bboxes = [
        BBox(x_min=200, y_min=int(y - 50), x_max=300, y_max=int(y + 50))
        for y in (1500, 1300, 1100, 900, 700, 600, 600, 600)
    ]
    obs = [_obs(i * 0.05, "white", b) for i, b in enumerate(bboxes)]
    ev = TextEvent(color_key="white", observations=obs)
    kind, dur = classify_entrance(ev, frame_w=1080, frame_h=1920)
    assert kind == "slide-up"
    assert dur > 0


def test_classify_entrance_fade_in_detected():
    bbox = BBox(x_min=200, y_min=900, x_max=300, y_max=1000)
    obs = [
        _obs(i * 0.05, "white", bbox, pixels=1000, brightness=b)
        for i, b in enumerate([0.1, 0.2, 0.4, 0.6, 0.8, 0.9, 0.9, 0.9])
    ]
    ev = TextEvent(color_key="white", observations=obs)
    kind, _ = classify_entrance(ev, frame_w=1080, frame_h=1920)
    assert kind == "fade-in"


def test_classify_entrance_font_cycle_detected():
    bbox = BBox(x_min=300, y_min=860, x_max=700, y_max=1060)
    pixels_seq = [2000, 3500, 1800, 4000, 2200, 3800, 1900, 3600]
    obs = [
        _obs(i * 0.05, "maize", bbox, pixels=p, brightness=0.9)
        for i, p in enumerate(pixels_seq)
    ]
    ev = TextEvent(color_key="maize", observations=obs)
    kind, _ = classify_entrance(ev, frame_w=1080, frame_h=1920)
    assert kind == "font-cycle"


def test_classify_entrance_static_when_nothing_changes():
    bbox = BBox(x_min=200, y_min=900, x_max=300, y_max=1000)
    obs = [_obs(i * 0.05, "white", bbox, pixels=1000, brightness=0.8) for i in range(8)]
    ev = TextEvent(color_key="white", observations=obs)
    kind, _ = classify_entrance(ev, frame_w=1080, frame_h=1920)
    assert kind == "static"


def test_classify_entrance_scale_up_detected():
    bbox = BBox(x_min=200, y_min=900, x_max=300, y_max=1000)
    # Strictly-monotonic pixel growth from 500 to 2000.
    pixels_seq = [500, 700, 900, 1200, 1500, 1700, 1900, 2000]
    obs = [
        _obs(i * 0.05, "white", bbox, pixels=p, brightness=0.8)
        for i, p in enumerate(pixels_seq)
    ]
    ev = TextEvent(color_key="white", observations=obs)
    kind, _ = classify_entrance(ev, frame_w=1080, frame_h=1920)
    assert kind == "scale-up"


def test_cooccurrence_intervals_visible_when_all_present():
    ev_this = TextEvent(color_key="white", observations=[
        _obs(0.0, "white", WHITE_LEFT), _obs(2.0, "white", WHITE_LEFT)])
    ev_this.text = "This"
    ev_is = TextEvent(color_key="white", observations=[
        _obs(0.5, "white", WHITE_RIGHT), _obs(2.0, "white", WHITE_RIGHT)])
    ev_is.text = "is"
    ev_africa = TextEvent(color_key="maize", observations=[
        _obs(1.0, "maize", MAIZE_CENTER), _obs(3.0, "maize", MAIZE_CENTER)])
    ev_africa.text = "Africa"

    intervals = cooccurrence_intervals([ev_this, ev_is, ev_africa])
    # At t=1.5 (within [1.0, 2.0]), all three should be visible.
    interval_at_1_5 = [iv for iv in intervals if iv[0] <= 1.5 <= iv[1]]
    assert interval_at_1_5
    visible = interval_at_1_5[0][2]
    assert {"This", "is", "Africa"}.issubset(visible)


def test_text_event_median_bbox_smooths_jitter():
    bboxes = [
        BBox(x_min=98, y_min=200, x_max=198, y_max=250),
        BBox(x_min=100, y_min=200, x_max=200, y_max=250),
        BBox(x_min=102, y_min=200, x_max=202, y_max=250),
    ]
    obs = [_obs(i * 0.05, "white", b) for i, b in enumerate(bboxes)]
    ev = TextEvent(color_key="white", observations=obs)
    med = ev.median_bbox()
    assert med.x_min == 100
    assert med.x_max == 200


def test_text_event_peak_observation_picks_highest_pixel_count():
    obs = [
        _obs(0.0, "white", WHITE_LEFT, pixels=500),
        _obs(0.05, "white", WHITE_LEFT, pixels=2000),
        _obs(0.10, "white", WHITE_LEFT, pixels=800),
    ]
    ev = TextEvent(color_key="white", observations=obs)
    assert ev.peak_observation().mask_pixel_count == 2000
