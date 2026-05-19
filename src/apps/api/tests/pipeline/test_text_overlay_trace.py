"""Tests for the overlay/render_window events emitted by
``_collect_absolute_overlays`` in template_orchestrate.py.

The admin job-debug timeline reads these events to show every text overlay
with its exact post-merge / post-clamp / post-override absolute window —
the same window that gets burned into the rendered video. These tests
guard the contract:

  - one event per surviving overlay
  - merged overlays carry the union of their source slot positions
  - curtain-close clamps surface as clamped_by="curtain_close"
  - timing overrides surface as clamped_by="override"
  - overlap truncation surfaces as clamped_by="overlap_truncated"
  - the internal _origin_slots / _clamped_by keys never leak to renderers

The function is called inside ``pipeline_trace_for(job_id)`` at runtime;
in tests we patch ``record_pipeline_event`` directly so we can assert the
event payload without standing up a real DB.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.tasks.template_orchestrate import _collect_absolute_overlays


def _step(position: int, overlays: list[dict], duration_s: float = 3.0) -> dict:
    return {
        "slot": {
            "position": position,
            "target_duration_s": duration_s,
            "text_overlays": overlays,
        },
        "clip_id": f"clip-{position}",
        "moment": {"start_s": 0.0, "end_s": duration_s},
    }


def _capture_events():
    """Patch the lazy-imported record_pipeline_event so we can read its
    call args. Returns (patcher_context, captured_list)."""
    captured: list[dict] = []

    def _fake(stage: str, event: str, data: dict | None = None) -> None:
        captured.append({"stage": stage, "event": event, "data": data or {}})

    return (
        patch(
            "app.services.pipeline_trace.record_pipeline_event",
            side_effect=_fake,
        ),
        captured,
    )


def test_emits_one_event_per_surviving_overlay():
    steps = [
        _step(
            1,
            [
                {
                    "role": "label",
                    "sample_text": "WELCOME",
                    "subject_substitute": False,
                    "start_s": 0.0,
                    "end_s": 2.0,
                    "position": "center",
                    "text_size": "large",
                }
            ],
        ),
        _step(
            2,
            [
                {
                    "role": "label",
                    "sample_text": "TO MOROCCO",
                    "subject_substitute": False,
                    "start_s": 0.0,
                    "end_s": 2.0,
                    "position": "center",
                    "position_y_frac": 0.6,  # different y so no merge
                    "text_size": "large",
                }
            ],
        ),
    ]

    patcher, captured = _capture_events()
    with patcher:
        result = _collect_absolute_overlays(
            steps,
            slot_durations=[3.0, 3.0],
            clip_metas=None,
            subject="MOROCCO",
        )

    assert len(result) == 2
    render_events = [e for e in captured if e["event"] == "render_window"]
    assert len(render_events) == 2

    # abs_start/end are slot_start + relative
    second = next(e for e in render_events if e["data"]["text"] == "TO MOROCCO")
    assert second["data"]["abs_start_s"] == pytest.approx(3.0)
    assert second["data"]["abs_end_s"] == pytest.approx(5.0)
    assert second["data"]["slot_index"] == 2
    # merged_from_slots is None for single-origin overlays so the timeline
    # UI can tell apart "from slot 2" from "merged across [1, 2]".
    assert second["data"]["merged_from_slots"] is None


def test_merge_unions_origin_slots():
    """Same text + same screen slot on adjacent slots with a gap < 2s
    merges into one overlay; the event lists both source positions."""
    steps = [
        _step(
            1,
            [
                {
                    "role": "label",
                    "sample_text": "WELCOME",
                    "start_s": 0.5,
                    "end_s": 2.8,
                    "position": "center",
                    "position_y_frac": 0.5,
                    "text_size": "medium",
                    "font_style": "display",
                }
            ],
        ),
        _step(
            2,
            [
                {
                    "role": "label",
                    "sample_text": "WELCOME",
                    "start_s": 0.0,
                    "end_s": 2.0,
                    "position": "center",
                    "position_y_frac": 0.5,
                    "text_size": "medium",
                    "font_style": "display",
                }
            ],
        ),
    ]

    patcher, captured = _capture_events()
    with patcher:
        result = _collect_absolute_overlays(
            steps,
            slot_durations=[3.0, 3.0],
            clip_metas=None,
            subject="MOROCCO",
        )

    assert len(result) == 1
    render_events = [e for e in captured if e["event"] == "render_window"]
    assert len(render_events) == 1
    assert render_events[0]["data"]["merged_from_slots"] == [1, 2]


def test_override_marked_clamped_by_override():
    steps = [
        _step(
            1,
            [
                {
                    "role": "label",
                    "sample_text": "HOOK",
                    "subject_substitute": False,
                    "start_s": 0.0,
                    "end_s": 3.0,
                    "start_s_override": 0.5,
                    "end_s_override": 2.5,
                    "position": "center",
                }
            ],
        ),
    ]

    patcher, captured = _capture_events()
    with patcher:
        _collect_absolute_overlays(
            steps,
            slot_durations=[3.0],
            clip_metas=None,
            subject="X",
        )

    render_events = [e for e in captured if e["event"] == "render_window"]
    assert len(render_events) == 1
    data = render_events[0]["data"]
    assert data["abs_start_s"] == pytest.approx(0.5)
    assert data["abs_end_s"] == pytest.approx(2.5)
    assert data["clamped_by"] == "override"


def test_curtain_close_slot_emits_no_overlay_events():
    """Slots immediately preceding a curtain-close interstitial have their
    text PRE-burned onto the slot clip (see _pre_burn_curtain_slot_text) so
    the curtain animation can close OVER the text. _collect_absolute_overlays
    therefore skips overlays on those slots entirely — and so should the
    pipeline_trace render_window events.

    Locks in that the early-continue contract: a curtain-close in the
    interstitial_map for slot N suppresses both the overlay AND the trace
    event for slot N.
    """
    steps = [
        _step(
            1,
            [
                {
                    "role": "label",
                    "sample_text": "AFRICA",
                    "subject_substitute": False,
                    "start_s": 0.0,
                    "end_s": 3.0,
                    "position": "center",
                }
            ],
        ),
    ]
    interstitial_map = {
        1: {"type": "curtain-close", "hold_s": 1.0},
    }

    patcher, captured = _capture_events()
    with patcher:
        result = _collect_absolute_overlays(
            steps,
            slot_durations=[3.0],
            clip_metas=None,
            subject="X",
            interstitial_map=interstitial_map,
        )

    assert result == []
    render_events = [e for e in captured if e["event"] == "render_window"]
    assert render_events == []


def test_internal_keys_stripped_from_returned_overlays():
    """The downstream PNG/ASS renderers must never see _origin_slots or
    _clamped_by — they would either be ignored (harmless) or, if a future
    renderer reflects unknown keys into FFmpeg argv, become a bug source.
    """
    steps = [
        _step(
            1,
            [
                {
                    "role": "label",
                    "sample_text": "HOOK",
                    "subject_substitute": False,
                    "start_s": 0.0,
                    "end_s": 2.0,
                    "position": "center",
                }
            ],
        ),
    ]

    patcher, _ = _capture_events()
    with patcher:
        result = _collect_absolute_overlays(
            steps,
            slot_durations=[3.0],
            clip_metas=None,
            subject="X",
        )

    assert len(result) == 1
    assert "_origin_slots" not in result[0]
    assert "_clamped_by" not in result[0]


def test_overlap_truncation_marked_clamped_by_overlap():
    """Dedup 2 truncates the earlier of two overlapping same-screen-slot
    overlays so they never render simultaneously. The truncated overlay
    must report clamped_by='overlap_truncated' in the trace event so the
    admin timeline can show the operator which overlay got cut."""
    steps = [
        _step(
            1,
            [
                {
                    "role": "label",
                    "sample_text": "FIRST",
                    "subject_substitute": False,
                    "start_s": 0.0,
                    "end_s": 2.5,
                    "position": "center",
                    "position_y_frac": 0.5,
                    "font_style": "display",
                },
                {
                    "role": "label",
                    "sample_text": "SECOND",
                    "subject_substitute": False,
                    "start_s": 1.5,
                    "end_s": 3.0,
                    "position": "center",
                    "position_y_frac": 0.5,
                    "font_style": "display",
                },
            ],
            duration_s=3.0,
        ),
    ]

    patcher, captured = _capture_events()
    with patcher:
        result = _collect_absolute_overlays(
            steps,
            slot_durations=[3.0],
            clip_metas=None,
            subject="X",
        )

    assert len(result) == 2
    render_events = [e for e in captured if e["event"] == "render_window"]
    texts = {e["data"]["text"]: e["data"] for e in render_events}
    assert texts["FIRST"]["clamped_by"] == "overlap_truncated"
    # FIRST should have been clipped to just before SECOND starts (1.5 − 0.1)
    assert texts["FIRST"]["abs_end_s"] == pytest.approx(1.4)
    # SECOND is unchanged
    assert texts["SECOND"]["clamped_by"] is None


def test_no_events_emitted_when_no_overlays():
    """An empty result must not emit phantom events."""
    steps = [_step(1, [])]
    patcher, captured = _capture_events()
    with patcher:
        result = _collect_absolute_overlays(
            steps,
            slot_durations=[3.0],
            clip_metas=None,
            subject="X",
        )
    assert result == []
    render_events = [e for e in captured if e["event"] == "render_window"]
    assert render_events == []


def test_trace_failure_does_not_break_overlay_collection(caplog):
    """A pipeline_trace write failure must be swallowed — overlay collection
    is on the render hot path and any DB hiccup must not break a job."""
    steps = [
        _step(
            1,
            [
                {
                    "role": "label",
                    "sample_text": "HOOK",
                    "subject_substitute": False,
                    "start_s": 0.0,
                    "end_s": 2.0,
                    "position": "center",
                }
            ],
        ),
    ]

    with patch(
        "app.services.pipeline_trace.record_pipeline_event",
        side_effect=RuntimeError("DB down"),
    ):
        result = _collect_absolute_overlays(
            steps,
            slot_durations=[3.0],
            clip_metas=None,
            subject="X",
        )

    # Function still returned the resolved overlay; render proceeds.
    assert len(result) == 1
    assert result[0]["text"] == "HOOK"
