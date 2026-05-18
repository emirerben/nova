"""Unit tests for stage C: temporal grouping of per-frame OCR detections.

Each test names the property of the algorithm it pins. The fixture builder
`make_detection()` lives in conftest.py so phrase tests can share it.
"""

from __future__ import annotations

import pytest

from app.agents._schemas.text_overlay_pipeline import TextEvent
from app.pipeline.text_overlay_v2.grouping import (
    DEFAULT_IOU_MATCH,
    DEFAULT_JITTER_MAX_GAP_S,
    _normalize_for_match,
    group_detections_into_events,
)

from .conftest import make_detection

# ── Pure helpers ──────────────────────────────────────────────────────────────


def test_normalize_collapses_whitespace_and_casefolds():
    assert _normalize_for_match("Hello\tWorld") == "hello world"
    assert _normalize_for_match("  spaces   between  ") == "spaces between"
    assert _normalize_for_match("Don't") == _normalize_for_match("DON'T")


def test_normalize_handles_nfc_unicode():
    # 'é' as a single codepoint vs e + combining acute
    composed = "café"
    decomposed = "café"
    assert _normalize_for_match(composed) == _normalize_for_match(decomposed)


# ── Boundary cases ────────────────────────────────────────────────────────────


def test_empty_input_returns_empty():
    assert group_detections_into_events([]) == []


def test_single_detection_becomes_one_event():
    dets = [make_detection(1.0, "Hello")]
    events = group_detections_into_events(dets)
    assert len(events) == 1
    assert events[0].text == "Hello"
    assert events[0].start_t_s == 1.0
    assert events[0].end_t_s == 1.0
    assert events[0].mean_confidence == 1.0


# ── Cross-frame matching ──────────────────────────────────────────────────────


def test_same_text_across_consecutive_frames_collapses_to_one_event():
    dets = [
        make_detection(0.5, "It's"),
        make_detection(1.0, "It's"),
        make_detection(1.5, "It's"),
    ]
    events = group_detections_into_events(dets)
    assert len(events) == 1
    assert events[0].start_t_s == 0.5
    assert events[0].end_t_s == 1.5


def test_jitter_gap_within_threshold_stays_one_event():
    # Frame at 1.0s is "missing" — at 2fps this is one dropped frame.
    dets = [
        make_detection(0.5, "Hi"),
        make_detection(1.5, "Hi"),  # 1.0s gap, within default 1.0s threshold
    ]
    events = group_detections_into_events(dets)
    assert len(events) == 1
    assert events[0].start_t_s == 0.5
    assert events[0].end_t_s == 1.5


def test_jitter_gap_exceeding_threshold_splits_into_two_events():
    # 1.5s gap exceeds default 1.0s jitter threshold.
    dets = [
        make_detection(0.0, "Hi"),
        make_detection(1.5, "Hi"),
    ]
    events = group_detections_into_events(dets)
    assert len(events) == 2
    assert events[0].end_t_s == 0.0
    assert events[1].start_t_s == 1.5


def test_different_text_in_same_frame_produces_separate_events():
    dets = [
        make_detection(1.0, "Top", y_center=0.2),
        make_detection(1.0, "Bottom", y_center=0.8),
    ]
    events = group_detections_into_events(dets)
    assert len(events) == 2
    texts = {e.text for e in events}
    assert texts == {"Top", "Bottom"}


def test_same_text_at_different_positions_produces_separate_events():
    # A watermark "Live" at the corner is NOT the same event as a hook "Live"
    # at center. The IoU check defends against this collision.
    dets = [
        make_detection(1.0, "Live", x_center=0.1, y_center=0.1, w=0.1, h=0.03),
        make_detection(1.0, "Live", x_center=0.5, y_center=0.5, w=0.3, h=0.08),
    ]
    events = group_detections_into_events(dets)
    assert len(events) == 2


def test_case_difference_still_matches_across_frames():
    # OCR sometimes flickers casing across frames; "Its" → "It's" → "ITS".
    # The casefold normalization keeps them as one event.
    dets = [
        make_detection(0.5, "It's"),
        make_detection(1.0, "IT'S"),
        make_detection(1.5, "it's"),
    ]
    events = group_detections_into_events(dets)
    assert len(events) == 1
    # The original casing of the FIRST detection is preserved as the event text.
    assert events[0].text == "It's"


def test_text_reappearing_after_other_text_starts_new_event():
    # "Hi" appears, then "Bye" appears in the same spot, then "Hi" comes back —
    # both "Hi" events should be distinct in time.
    dets = [
        make_detection(0.0, "Hi"),
        make_detection(0.5, "Hi"),
        make_detection(1.0, "Bye"),  # screen-replaces "Hi" at same X-position
        make_detection(1.5, "Bye"),
        make_detection(3.0, "Hi"),  # past jitter gap → new event
    ]
    events = group_detections_into_events(dets)
    hi_events = [e for e in events if e.text == "Hi"]
    assert len(hi_events) == 2
    assert hi_events[0].start_t_s == 0.0
    assert hi_events[0].end_t_s == 0.5
    assert hi_events[1].start_t_s == 3.0
    assert hi_events[1].end_t_s == 3.0


def test_event_aabb_is_union_of_constituent_frame_bboxes():
    # Caption shifts position slightly across frames (OCR jitter on bbox edges).
    # The event's aabb should encompass both positions.
    dets = [
        make_detection(0.0, "Hi", x_center=0.5, y_center=0.5, w=0.1, h=0.05),
        make_detection(0.5, "Hi", x_center=0.52, y_center=0.51, w=0.1, h=0.05),
    ]
    events = group_detections_into_events(dets)
    assert len(events) == 1
    x_min, y_min, x_max, y_max = events[0].aabb
    # Encompasses both bboxes
    assert x_min == pytest.approx(0.45)
    assert x_max == pytest.approx(0.57)
    assert y_min == pytest.approx(0.475)
    assert y_max == pytest.approx(0.535)


def test_event_mean_confidence_is_frame_weighted():
    # Three frames at confidence 0.9, 1.0, 0.8 → mean = 0.9
    dets = [
        make_detection(0.0, "Hi", confidence=0.9),
        make_detection(0.5, "Hi", confidence=1.0),
        make_detection(1.0, "Hi", confidence=0.8),
    ]
    events = group_detections_into_events(dets)
    assert events[0].mean_confidence == pytest.approx(0.9)


def test_output_sorted_by_start_then_y():
    # Input is intentionally shuffled across time; output must be ordered.
    dets = [
        make_detection(2.0, "Late"),
        make_detection(0.0, "Top", y_center=0.2),
        make_detection(0.0, "Bottom", y_center=0.8),
    ]
    events = group_detections_into_events(dets)
    assert [e.text for e in events] == ["Top", "Bottom", "Late"]


# ── Configurable thresholds ───────────────────────────────────────────────────


def test_jitter_threshold_is_tunable():
    # With default 1.0s, the 0.8s gap is bridged.
    dets = [make_detection(0.0, "Hi"), make_detection(0.8, "Hi")]
    assert len(group_detections_into_events(dets)) == 1
    # With tighter 0.5s, the same gap splits.
    assert len(group_detections_into_events(dets, jitter_max_gap_s=0.5)) == 2


def test_iou_threshold_is_tunable():
    # Two bboxes overlap by IoU ~= 0.18 (small overlap).
    dets = [
        make_detection(0.0, "Hi", x_center=0.5, y_center=0.5, w=0.2, h=0.05),
        make_detection(0.5, "Hi", x_center=0.65, y_center=0.5, w=0.2, h=0.05),
    ]
    # With default 0.3 threshold, these DON'T match.
    assert len(group_detections_into_events(dets)) == 2
    # With looser 0.1 threshold, they DO.
    assert len(group_detections_into_events(dets, iou_match_threshold=0.1)) == 1


def test_thresholds_use_module_constants_by_default():
    # Smoke test that the defaults are wired through correctly.
    assert DEFAULT_IOU_MATCH == 0.3
    assert DEFAULT_JITTER_MAX_GAP_S == 1.0


# ── Output schema invariants ──────────────────────────────────────────────────


def test_output_events_satisfy_schema_constraints():
    dets = [make_detection(0.0, "Hi"), make_detection(0.5, "Hi")]
    events = group_detections_into_events(dets)
    for e in events:
        assert isinstance(e, TextEvent)
        assert e.text  # non-empty
        assert e.start_t_s >= 0.0
        assert e.end_t_s >= e.start_t_s
        x0, y0, x1, y1 = e.aabb
        assert 0.0 <= x0 <= x1 <= 1.0
        assert 0.0 <= y0 <= y1 <= 1.0
        assert 0.0 <= e.mean_confidence <= 1.0
