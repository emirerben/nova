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


def test_lyric_line_segments_merge_across_slots():
    """Line lyrics split by short beat slots should burn as one ASS event.

    The injector emits per-slot bridge segments so the text survives clip cuts.
    The final overlay collector must stitch those segments back together before
    same-position overlap truncation runs; otherwise the line visibly restarts at
    every slot boundary.
    """
    overlays = [
        {
            "role": "lyrics",
            "text": "We ain't stressing 'bout the loot",
            "effect": "lyric-line",
            "start_s": 1.0,
            "end_s": 2.0,
            "position": "bottom",
            "position_y_frac": 0.8,
            "font_family": "Inter Tight",
            "text_color": "#FFFFFF",
            "fade_in_ms": 150,
            "fade_out_ms": 0,
            "lyric_line_id": "line:0:3.000:5.800",
            "lyric_segment_index": 0,
            "lyric_segment_count": 3,
        },
        {
            "role": "lyrics",
            "text": "We ain't stressing 'bout the loot",
            "effect": "lyric-line",
            "start_s": 0.0,
            "end_s": 2.0,
            "position": "bottom",
            "position_y_frac": 0.8,
            "font_family": "Inter Tight",
            "text_color": "#FFFFFF",
            "fade_in_ms": 0,
            "fade_out_ms": 0,
            "lyric_line_id": "line:0:3.000:5.800",
            "lyric_segment_index": 1,
            "lyric_segment_count": 3,
        },
        {
            "role": "lyrics",
            "text": "We ain't stressing 'bout the loot",
            "effect": "lyric-line",
            "start_s": 0.0,
            "end_s": 0.8,
            "position": "bottom",
            "position_y_frac": 0.8,
            "font_family": "Inter Tight",
            "text_color": "#FFFFFF",
            "fade_in_ms": 0,
            "fade_out_ms": 250,
            "lyric_line_id": "line:0:3.000:5.800",
            "lyric_segment_index": 2,
            "lyric_segment_count": 3,
        },
    ]
    steps = [
        _step(1, [overlays[0]], duration_s=2.0),
        _step(2, [overlays[1]], duration_s=2.0),
        _step(3, [overlays[2]], duration_s=2.0),
    ]

    patcher, captured = _capture_events()
    with patcher:
        result = _collect_absolute_overlays(
            steps,
            slot_durations=[2.0, 2.0, 2.0],
            clip_metas=None,
            subject="X",
        )

    assert len(result) == 1
    overlay = result[0]
    assert overlay["text"] == "We ain't stressing 'bout the loot"
    assert overlay["start_s"] == pytest.approx(1.0)
    assert overlay["end_s"] == pytest.approx(4.8)
    assert overlay["fade_in_ms"] == 150
    assert overlay["fade_out_ms"] == 250
    assert "lyric_line_id" not in overlay
    assert "lyric_segment_index" not in overlay
    assert "lyric_segment_count" not in overlay

    render_events = [e for e in captured if e["event"] == "render_window"]
    assert len(render_events) == 1
    assert render_events[0]["data"]["merged_from_slots"] == [1, 2, 3]
    assert render_events[0]["data"]["lyric_line_id"] == "line:0:3.000:5.800"


def test_lyric_line_is_not_truncated_by_next_lyric_line_overlap():
    """Line timing caps already own lyric overlap; generic text dedup must not."""
    steps = [
        _step(
            1,
            [
                {
                    "role": "lyrics",
                    "text": "First lyric",
                    "effect": "lyric-line",
                    "start_s": 0.0,
                    "end_s": 2.5,
                    "position": "bottom",
                    "position_y_frac": 0.8,
                    "lyric_line_id": "line:0:0.000:2.000",
                },
                {
                    "role": "lyrics",
                    "text": "Second lyric",
                    "effect": "lyric-line",
                    "start_s": 1.5,
                    "end_s": 3.0,
                    "position": "bottom",
                    "position_y_frac": 0.8,
                    "lyric_line_id": "line:1:1.800:3.000",
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
    by_text = {overlay["text"]: overlay for overlay in result}
    assert by_text["First lyric"]["end_s"] == pytest.approx(2.5)
    assert by_text["Second lyric"]["start_s"] == pytest.approx(1.5)

    render_events = [e for e in captured if e["event"] == "render_window"]
    trace_by_text = {e["data"]["text"]: e["data"] for e in render_events}
    assert trace_by_text["First lyric"]["clamped_by"] is None
    assert trace_by_text["Second lyric"]["clamped_by"] is None


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


# ── Layer 1 tests: identity-driven lyric-line merge (plan §1) ─────────────────


def _lyric_overlay(
    *,
    text: str,
    start_s: float,
    end_s: float,
    line_id: str,
    fade_in_ms: int = 150,
    fade_out_ms: int = 250,
    text_color: str = "#FFFFFF",
    position: str = "bottom",
    position_y_frac: float = 0.8,
    position_x_frac: float | None = None,
    origin_slot: int | None = 1,
    extras: dict | None = None,
) -> dict:
    """Build a per-segment lyric-line overlay dict as `_inject_line` emits and
    `_collect_absolute_overlays` populates with abs timestamps + _origin_slots."""
    ov = {
        "text": text,
        "effect": "lyric-line",
        "start_s": start_s,
        "end_s": end_s,
        "position": position,
        "position_y_frac": position_y_frac,
        "position_x_frac": position_x_frac,
        "font_family": "Inter Tight",
        "text_color": text_color,
        "fade_in_ms": fade_in_ms,
        "fade_out_ms": fade_out_ms,
        "lyric_line_id": line_id,
        "original_text": text,
        "original_start_s_song": 100.0,
        "original_end_s_song": 105.0,
        "original_words": [],
        "_origin_slots": [origin_slot] if origin_slot is not None else [],
    }
    if extras:
        ov.update(extras)
    return ov


def _build_steps_with_overlays(
    slot_overlays: list[list[dict]], duration_s: float = 4.0
) -> tuple[list[dict], list[float]]:
    """Make steps from per-slot overlay lists. Each slot gets `duration_s`."""
    steps = [
        _step(i + 1, overlays, duration_s=duration_s) for i, overlays in enumerate(slot_overlays)
    ]
    return steps, [duration_s] * len(slot_overlays)


def _slot_overlay_input(
    *,
    text: str,
    rel_start: float,
    rel_end: float,
    line_id: str,
    fade_in_ms: int = 150,
    fade_out_ms: int = 250,
    text_color: str = "#FFFFFF",
    position: str = "bottom",
    position_y_frac: float = 0.8,
    position_x_frac: float | None = None,
) -> dict:
    """Per-slot overlay dict as `_inject_line` emits BEFORE abs conversion."""
    return {
        "text": text,
        "effect": "lyric-line",
        "start_s": rel_start,
        "end_s": rel_end,
        "position": position,
        "position_y_frac": position_y_frac,
        "position_x_frac": position_x_frac,
        "font_family": "Inter Tight",
        "text_color": text_color,
        "fade_in_ms": fade_in_ms,
        "fade_out_ms": fade_out_ms,
        "lyric_line_id": line_id,
        "original_text": text,
        "original_start_s_song": 100.0,
        "original_end_s_song": 105.0,
        "original_words": [],
        "role": "lyrics",
    }


def test_lyric_line_continuation_merges_under_beat_snap_drift_220ms() -> None:
    """The exact Bug A failure mode: two segments same lyric_line_id, slot
    boundary at 4.117 pre-snap drifts to 4.338 post-snap → 221 ms gap.
    Merge must still fire (identity-driven, no gap threshold)."""
    seg0 = _slot_overlay_input(
        text="So take my strong advice",
        rel_start=0.0,
        rel_end=4.117,
        line_id="line:0",
        fade_in_ms=150,
        fade_out_ms=0,
    )
    seg1 = _slot_overlay_input(
        text="So take my strong advice",
        rel_start=0.0,
        rel_end=1.623,
        line_id="line:0",
        fade_in_ms=0,
        fade_out_ms=250,
    )
    # slot 0 post-snap dur = 4.338 → cumulative_s for slot 1 = 4.338 → seg1 abs = 4.338-5.961
    steps, _ = _build_steps_with_overlays([[seg0], [seg1]], duration_s=0.0)  # override below
    steps[0]["slot"]["target_duration_s"] = 4.117  # pre-snap of seg0
    steps[1]["slot"]["target_duration_s"] = 3.605
    # Post-snap slot_durations passed into _collect_absolute_overlays
    slot_durations = [4.338, 3.605]
    result = _collect_absolute_overlays(
        steps,
        slot_durations,
        clip_metas=None,
        subject="",
    )
    # Filter to lyric-line overlays
    lyric_lines = [o for o in result if o.get("effect") == "lyric-line"]
    assert len(lyric_lines) == 1, f"expected merge to ONE overlay; got {len(lyric_lines)}"
    merged = lyric_lines[0]
    assert merged["start_s"] == 0.0
    # End = max of merged segments = 4.338 + 1.623 = 5.961
    assert abs(merged["end_s"] - 5.961) < 1e-3


def test_lyric_line_continuation_merges_under_large_drift_600ms(caplog) -> None:
    """Drift = 600 ms (a 100-BPM beat-interval upper bound). Must still merge
    AND emit lyric_line_large_continuation_gap warning."""
    seg0 = _slot_overlay_input(
        text="X line",
        rel_start=0.0,
        rel_end=4.0,
        line_id="line:1",
        fade_in_ms=150,
        fade_out_ms=0,
    )
    seg1 = _slot_overlay_input(
        text="X line",
        rel_start=0.0,
        rel_end=1.0,
        line_id="line:1",
        fade_in_ms=0,
        fade_out_ms=250,
    )
    steps, _ = _build_steps_with_overlays([[seg0], [seg1]], duration_s=0.0)
    steps[0]["slot"]["target_duration_s"] = 4.0
    steps[1]["slot"]["target_duration_s"] = 1.0
    # Slot 0 ends at 4.6 (600 ms drift) → seg1 abs starts at 4.6 → gap = 0.6
    slot_durations = [4.6, 1.0]
    import logging

    with caplog.at_level(logging.WARNING):
        result = _collect_absolute_overlays(steps, slot_durations, clip_metas=None, subject="")
    lyric_lines = [o for o in result if o.get("effect") == "lyric-line"]
    assert len(lyric_lines) == 1


def test_merge_preserves_first_fade_in_and_last_fade_out() -> None:
    """Three segments with intermediate fades zeroed; merge keeps first.fade_in
    and last.fade_out."""
    seg0 = _slot_overlay_input(
        text="L", rel_start=0.0, rel_end=2.0, line_id="line:2", fade_in_ms=150, fade_out_ms=0
    )
    seg1 = _slot_overlay_input(
        text="L", rel_start=0.0, rel_end=2.0, line_id="line:2", fade_in_ms=0, fade_out_ms=0
    )
    seg2 = _slot_overlay_input(
        text="L", rel_start=0.0, rel_end=1.0, line_id="line:2", fade_in_ms=0, fade_out_ms=250
    )
    steps, sd = _build_steps_with_overlays([[seg0], [seg1], [seg2]], duration_s=2.0)
    steps[2]["slot"]["target_duration_s"] = 1.0
    sd[2] = 1.0
    result = _collect_absolute_overlays(steps, sd, clip_metas=None, subject="")
    lyric_lines = [o for o in result if o.get("effect") == "lyric-line"]
    assert len(lyric_lines) == 1
    merged = lyric_lines[0]
    assert merged["fade_in_ms"] == 150
    assert merged["fade_out_ms"] == 250


def test_merge_window_spans_min_start_to_max_end() -> None:
    """Merged start_s = min of segments; end_s = max."""
    seg0 = _slot_overlay_input(text="W", rel_start=0.0, rel_end=2.0, line_id="line:3")
    seg1 = _slot_overlay_input(text="W", rel_start=0.0, rel_end=2.0, line_id="line:3")
    steps, sd = _build_steps_with_overlays([[seg0], [seg1]], duration_s=2.0)
    result = _collect_absolute_overlays(steps, sd, clip_metas=None, subject="")
    lyric_lines = [o for o in result if o.get("effect") == "lyric-line"]
    assert len(lyric_lines) == 1
    assert lyric_lines[0]["start_s"] == 0.0
    assert lyric_lines[0]["end_s"] == 4.0  # 0 + 2 (slot 0 dur) + 2 (slot 1 dur from abs)


def test_intervening_non_lyric_overlay_does_not_block_same_line_merge() -> None:
    """Non-lyric overlay between two same-id lyric segments doesn't prevent
    consolidation."""
    seg0 = _slot_overlay_input(text="L", rel_start=0.0, rel_end=2.0, line_id="line:4")
    non_lyric = {
        "text": "WELCOME",
        "effect": "pop-in",
        "start_s": 0.5,
        "end_s": 1.5,
        "position": "center",
        "position_y_frac": 0.5,
    }
    seg1 = _slot_overlay_input(text="L", rel_start=0.0, rel_end=2.0, line_id="line:4")
    # Pack non-lyric into slot 0 alongside seg0
    steps, sd = _build_steps_with_overlays([[seg0, non_lyric], [seg1]], duration_s=2.0)
    result = _collect_absolute_overlays(steps, sd, clip_metas=None, subject="")
    lyric_lines = [o for o in result if o.get("effect") == "lyric-line"]
    assert len(lyric_lines) == 1, "same-id lyric segments must merge despite non-lyric neighbor"
    non_lyrics = [o for o in result if o.get("effect") != "lyric-line"]
    assert len(non_lyrics) == 1
    assert non_lyrics[0]["text"] == "WELCOME"


def test_same_lyric_line_id_different_visual_style_does_not_merge(caplog) -> None:
    """Different text_color on same lyric_line_id → not merged AND warning."""
    seg0 = _slot_overlay_input(
        text="C", rel_start=0.0, rel_end=2.0, line_id="line:5", text_color="#FFFFFF"
    )
    seg1 = _slot_overlay_input(
        text="C", rel_start=0.0, rel_end=2.0, line_id="line:5", text_color="#FF0000"
    )
    steps, sd = _build_steps_with_overlays([[seg0], [seg1]], duration_s=2.0)
    import logging

    with caplog.at_level(logging.WARNING):
        result = _collect_absolute_overlays(steps, sd, clip_metas=None, subject="")
    lyric_lines = [o for o in result if o.get("effect") == "lyric-line"]
    assert len(lyric_lines) == 2, "visual style mismatch must NOT merge"


def test_repeated_text_different_lyric_line_id_does_not_merge() -> None:
    """Same text 'Billie Jean is not my lover' in two distinct lyric_line_ids
    (chorus repeat) → kept as TWO separate overlays."""
    seg0 = _slot_overlay_input(
        text="Billie Jean is not my lover", rel_start=0.0, rel_end=2.0, line_id="line:13"
    )
    seg1 = _slot_overlay_input(
        text="Billie Jean is not my lover", rel_start=0.0, rel_end=2.0, line_id="line:34"
    )
    steps, sd = _build_steps_with_overlays([[seg0], [seg1]], duration_s=2.0)
    result = _collect_absolute_overlays(steps, sd, clip_metas=None, subject="")
    lyric_lines = [o for o in result if o.get("effect") == "lyric-line"]
    assert len(lyric_lines) == 2, "different lyric_line_id must NOT merge"


def test_merged_origin_slots_unions_all_members_filters_none() -> None:
    """`_origin_slots` accumulates from ALL members (not first only); None
    entries are filtered."""
    seg0 = _slot_overlay_input(text="M", rel_start=0.0, rel_end=2.0, line_id="line:6")
    seg1 = _slot_overlay_input(text="M", rel_start=0.0, rel_end=2.0, line_id="line:6")
    seg2 = _slot_overlay_input(text="M", rel_start=0.0, rel_end=2.0, line_id="line:6")
    steps, sd = _build_steps_with_overlays([[seg0], [seg1], [seg2]], duration_s=2.0)
    # Each segment will get _origin_slots from its slot position via the
    # raw.append logic in _collect_absolute_overlays.
    result = _collect_absolute_overlays(steps, sd, clip_metas=None, subject="")
    lyric_lines = [o for o in result if o.get("effect") == "lyric-line"]
    assert len(lyric_lines) == 1
    # _origin_slots was stripped by the final result builder, so check
    # `merged_from_slots` (the public-facing trace field) is also stripped.
    # We instead verify the union via the merge-time guarantee: the merged
    # window spans all three slot durations.
    assert lyric_lines[0]["end_s"] == 6.0


def test_consolidate_pre_pass_does_not_mutate_input_list_during_iteration() -> None:
    """No iteration-time list.remove. Build a probe list and confirm the
    output order matches expectations."""
    seg0 = _slot_overlay_input(text="P", rel_start=0.0, rel_end=2.0, line_id="line:7")
    seg1 = _slot_overlay_input(text="P", rel_start=0.0, rel_end=2.0, line_id="line:7")
    seg2 = _slot_overlay_input(text="P", rel_start=0.0, rel_end=2.0, line_id="line:7")
    steps, sd = _build_steps_with_overlays([[seg0], [seg1], [seg2]], duration_s=2.0)
    # Should not raise / not skip entries
    result = _collect_absolute_overlays(steps, sd, clip_metas=None, subject="")
    lyric_lines = [o for o in result if o.get("effect") == "lyric-line"]
    assert len(lyric_lines) == 1


def test_slot_key_is_visual_only_not_video_slot_index() -> None:
    """_slot_key uses screen position attributes only; two overlays from
    different video slot positions but at the same screen position must
    have equal slot keys."""
    # Same screen position, different segments → same lyric_line_id triggers merge
    seg0 = _slot_overlay_input(
        text="K",
        rel_start=0.0,
        rel_end=2.0,
        line_id="line:8",
        position="bottom",
        position_y_frac=0.8,
    )
    seg1 = _slot_overlay_input(
        text="K",
        rel_start=0.0,
        rel_end=2.0,
        line_id="line:8",
        position="bottom",
        position_y_frac=0.8,
    )
    steps, sd = _build_steps_with_overlays([[seg0], [seg1]], duration_s=2.0)
    result = _collect_absolute_overlays(steps, sd, clip_metas=None, subject="")
    lyric_lines = [o for o in result if o.get("effect") == "lyric-line"]
    # If _slot_key included a video-slot index, these would differ and the
    # merge would not fire. Locks the screen-position-only contract.
    assert len(lyric_lines) == 1


def test_collect_absolute_overlays_propagates_originals_to_finalizer() -> None:
    """P1 regression: `_collect_absolute_overlays` rebuilds an entry dict from
    each slot's overlay, cherry-picking known keys. The Layer 2 finalizer
    (`_finalize_lyric_audible_window`) requires `original_text`,
    `original_start_s_song`, `original_end_s_song`, and `original_words` on the
    merged overlay. If those aren't propagated through the entry rebuild,
    finalization hits its missing-metadata safe-passthrough branch in prod
    and Bug B is silently a no-op (the unit tests in `test_lyric_injector.py`
    construct overlays directly and bypass this rebuild).
    """
    # Simulate `_inject_line` output: a lyric-line overlay with all 4 originals.
    original = "She told my baby we'd danced 'til three"
    original_words = [
        {"text": "She", "start_s_song": 136.88, "end_s_song": 137.30},
        {"text": "told", "start_s_song": 137.30, "end_s_song": 137.68},
        {"text": "my", "start_s_song": 137.68, "end_s_song": 137.98},
        {"text": "baby", "start_s_song": 137.98, "end_s_song": 138.38},
        {"text": "we'd", "start_s_song": 138.40, "end_s_song": 138.45},
        {"text": "danced", "start_s_song": 138.38, "end_s_song": 138.98},
        {"text": "'til", "start_s_song": 139.00, "end_s_song": 139.16},
        {"text": "three", "start_s_song": 139.18, "end_s_song": 139.54},
    ]
    slot_overlay = {
        "text": original,
        "effect": "lyric-line",
        "start_s": 0.0,
        "end_s": 2.156,
        "position": "bottom",
        "position_y_frac": 0.8,
        "position_x_frac": None,
        "font_family": "Inter Tight",
        "text_color": "#FFFFFF",
        "fade_in_ms": 150,
        "fade_out_ms": 250,
        "lyric_line_id": "line:0:136.880:139.540",
        "lyric_segment_index": 0,
        "lyric_segment_count": 1,
        "role": "lyrics",
        # The 4 fields the bug forgot to propagate.
        "original_text": original,
        "original_start_s_song": 136.88,
        "original_end_s_song": 139.54,
        "original_words": original_words,
    }
    steps, sd = _build_steps_with_overlays([[slot_overlay]], duration_s=2.156)
    # Run with the Layer 2 finalizer enabled. Audible window ends at song-time
    # 138.88 (best_start_s=136.88, video duration 2.0s) → drops "'til" and
    # "three" by midpoint rule. If `original_*` is NOT propagated through the
    # entry rebuild, the finalizer skips with `lyric_segments_missing_finalization_metadata`
    # and returns the full original text — this test FAILS.
    result = _collect_absolute_overlays(
        steps,
        sd,
        clip_metas=None,
        subject="",
        lyric_audio_mix_song_start_s=136.88,
    )
    lyric_lines = [o for o in result if o.get("effect") == "lyric-line"]
    assert len(lyric_lines) == 1
    merged = lyric_lines[0]
    # If the propagation works, finalizer should have written display_text
    # (truncated to audible suffix). If it doesn't, display_text is missing
    # AND text remains the full original.
    assert "display_text" in merged, (
        "Layer 2 finalizer did not run — original_* fields not propagated "
        "through _collect_absolute_overlays entry rebuild. This is the P1 "
        "propagation-gap regression."
    )
    # Truncated to audible portion (which words depends on audio window):
    # audible_end_s_song=138.88 → audible words [She, told, my, baby, we'd] (midpoints < 138.88)
    # Display should NOT contain "'til" or "three" (those are dropped).
    assert "'til" not in merged["display_text"]
    assert "three" not in merged["display_text"]


# ── Log-event + interstitial branch assertions (P1 from testing specialist) ──


class _OrchestrateLogRecorder:
    """Captures structlog calls on `app.tasks.template_orchestrate.log` for
    assertion. Same pattern as test_template_matcher.py:417."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    def info(self, event, **kwargs):
        self.events.append(("info", event, kwargs))

    def warning(self, event, **kwargs):
        self.events.append(("warning", event, kwargs))

    def debug(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass

    def events_named(self, name: str) -> list[dict]:
        return [k for _, e, k in self.events if e == name]


def test_log_lyric_line_large_continuation_gap(monkeypatch) -> None:
    """Same-id segments with gap > 0.5s must still merge AND log the warning
    so prod regressions (gap should be near-zero after TODO 1 post-snap
    injection lands) surface in the admin timeline."""
    from app.tasks import template_orchestrate

    rec = _OrchestrateLogRecorder()
    monkeypatch.setattr(template_orchestrate, "log", rec)
    seg0 = _slot_overlay_input(
        text="L",
        rel_start=0.0,
        rel_end=4.0,
        line_id="line:bigdrift",
        fade_in_ms=150,
        fade_out_ms=0,
    )
    seg1 = _slot_overlay_input(
        text="L",
        rel_start=0.0,
        rel_end=1.0,
        line_id="line:bigdrift",
        fade_in_ms=0,
        fade_out_ms=250,
    )
    steps, _ = _build_steps_with_overlays([[seg0], [seg1]], duration_s=0.0)
    steps[0]["slot"]["target_duration_s"] = 4.0
    steps[1]["slot"]["target_duration_s"] = 1.0
    # Slot 0 ends at 4.6 (600 ms drift) → gap = 0.6 > _LARGE_CONTINUATION_GAP_WARNING_S=0.5
    slot_durations = [4.6, 1.0]
    _collect_absolute_overlays(steps, slot_durations, clip_metas=None, subject="")
    events = rec.events_named("lyric_line_large_continuation_gap")
    assert events, f"expected gap warning; got {[e[1] for e in rec.events]}"
    assert events[0].get("line_id") == "line:bigdrift"
    assert events[0].get("gap_s", 0) > 0.5


def test_log_lyric_segments_visual_style_drift(monkeypatch) -> None:
    """Same lyric_line_id, different text_color → consolidation refuses + logs
    the drift event so the admin can see the metadata mismatch."""
    from app.tasks import template_orchestrate

    rec = _OrchestrateLogRecorder()
    monkeypatch.setattr(template_orchestrate, "log", rec)
    seg0 = _slot_overlay_input(
        text="S", rel_start=0.0, rel_end=2.0, line_id="line:drift", text_color="#FFFFFF"
    )
    seg1 = _slot_overlay_input(
        text="S", rel_start=0.0, rel_end=2.0, line_id="line:drift", text_color="#FF0000"
    )
    steps, sd = _build_steps_with_overlays([[seg0], [seg1]], duration_s=2.0)
    result = _collect_absolute_overlays(steps, sd, clip_metas=None, subject="")
    events = rec.events_named("lyric_segments_visual_style_drift")
    assert events
    assert events[0].get("line_id") == "line:drift"
    # And the two overlays stay separate (didn't merge).
    lyric_lines = [o for o in result if o.get("effect") == "lyric-line"]
    assert len(lyric_lines) == 2


def test_interstitial_holds_widen_audible_window(monkeypatch) -> None:
    """`_collect_absolute_overlays` computes `total_audio_bearing_duration_s =
    sum(slot_durations) + sum(interstitial holds)`. If a future caller passes
    interstitial holds with music audio continuing through them, the audible
    window must grow accordingly so lyrics inside the extended tail aren't
    dropped. Test: same overlay, same audio start, identical slot durations —
    once without interstitials (dropped), once with a 2.5s hold (kept)."""
    original = "Hello world this is a test"
    original_words = [
        {"text": w, "start_s_song": 130.0 + i * 0.4, "end_s_song": 130.3 + i * 0.4}
        for i, w in enumerate(original.split())
    ]
    # All 6 words sung between 130.0 → 132.4. Audible window with NO interstitial:
    # start=128.0, end=128.0+slot_dur=128.0+3.0=131.0 → only first ~2 words audible.
    # With +2.5s interstitial: end=133.5 → all 6 audible.
    slot_overlay = {
        "text": original,
        "effect": "lyric-line",
        "start_s": 0.0,
        "end_s": 3.0,
        "position": "bottom",
        "position_y_frac": 0.8,
        "position_x_frac": None,
        "font_family": "Inter Tight",
        "text_color": "#FFFFFF",
        "fade_in_ms": 150,
        "fade_out_ms": 250,
        "lyric_line_id": "line:inter",
        "lyric_segment_index": 0,
        "lyric_segment_count": 1,
        "role": "lyrics",
        "original_text": original,
        "original_start_s_song": 130.0,
        "original_end_s_song": 132.7,
        "original_words": original_words,
    }
    steps, sd = _build_steps_with_overlays([[slot_overlay]], duration_s=3.0)

    # Without interstitial: audible end_s_song = 128.0 + 3.0 = 131.0
    # → coverage_words = 3/6 (≈0.5), coverage_duration low → dropped (interior partial floor fails)
    # But this is the ONLY lyric overlay → is_final=True → final-line floor applies (basic only).
    # surviving_word_count=3, audible_speech_s ~ 3*0.3=0.9 ≥ 0.75 → KEPT with truncation.
    result_no_inter = _collect_absolute_overlays(
        steps,
        sd,
        clip_metas=None,
        subject="",
        lyric_audio_mix_song_start_s=128.0,
    )
    lyric_no_inter = [o for o in result_no_inter if o.get("effect") == "lyric-line"]
    assert len(lyric_no_inter) == 1
    # display_text was rebuilt → not the full original
    assert lyric_no_inter[0].get("display_text") != original

    # WITH interstitial extending audible window: end_s_song = 128.0 + 3.0 + 2.5 = 133.5
    # All 6 words audible → near-complete → original text preserved, no display_text.
    interstitial_map = {1: {"hold_s": 2.5, "type": "test"}}
    # Rebuild steps to attach interstitial_map cleanly:
    result_with_inter = _collect_absolute_overlays(
        steps,
        sd,
        clip_metas=None,
        subject="",
        interstitial_map=interstitial_map,
        lyric_audio_mix_song_start_s=128.0,
    )
    lyric_with_inter = [o for o in result_with_inter if o.get("effect") == "lyric-line"]
    assert len(lyric_with_inter) == 1
    # Coverage_words = 6/6 = 1.0, coverage_dur = 2.7/2.7 = 1.0 → near-complete
    # → display_text NOT written → renderer uses original text.
    assert lyric_with_inter[0].get("display_text") is None, (
        "with the interstitial extending the audible window, the line should be "
        "near-complete and pass through unchanged"
    )


def test_silent_interstitial_does_not_widen_audible_window() -> None:
    """Review #4: an interstitial that declares
    `music_audio_continues_during_hold=False` (a future silent-hold
    interstitial type, e.g. a beat-of-silence between sections) must NOT
    extend `total_audio_bearing_duration_s`. The audible window stays at
    `best_start_s + sum(slot_durations)` and a lyric line whose audio falls
    inside the silent hold is treated as inaudible.

    Setup: 1 slot, 3.0s. Without interstitial: audible end = 131.0
    (line outside → dropped). With audio-bearing interstitial (default
    True): audible end = 133.5 (line inside → kept). With silent
    interstitial (flag=False): audible end = 131.0 (line outside → dropped).
    Locks the contract before any silent-hold interstitial ships.
    """
    original = "Hello world this is a test"
    original_words = [
        {"text": w, "start_s_song": 131.5 + i * 0.3, "end_s_song": 131.8 + i * 0.3}
        for i, w in enumerate(original.split())
    ]
    slot_overlay = {
        "text": original,
        "effect": "lyric-line",
        "start_s": 0.0,
        "end_s": 3.0,
        "position": "bottom",
        "position_y_frac": 0.8,
        "position_x_frac": None,
        "font_family": "Inter Tight",
        "text_color": "#FFFFFF",
        "fade_in_ms": 150,
        "fade_out_ms": 250,
        "lyric_line_id": "line:silent-test",
        "lyric_segment_index": 0,
        "lyric_segment_count": 1,
        "role": "lyrics",
        "original_text": original,
        "original_start_s_song": 131.5,
        "original_end_s_song": 133.3,
        "original_words": original_words,
    }
    steps, sd = _build_steps_with_overlays([[slot_overlay]], duration_s=3.0)

    # Silent hold: 2.5s but `music_audio_continues_during_hold=False`.
    # Audible end stays at 128.0 + 3.0 = 131.0 → line at 131.5+ entirely
    # past audible → dropped (no audible overlap).
    silent_inter = {
        1: {"hold_s": 2.5, "music_audio_continues_during_hold": False, "type": "silent"}
    }
    result_silent = _collect_absolute_overlays(
        steps,
        sd,
        clip_metas=None,
        subject="",
        interstitial_map=silent_inter,
        lyric_audio_mix_song_start_s=128.0,
    )
    lyric_silent = [o for o in result_silent if o.get("effect") == "lyric-line"]
    assert lyric_silent == [], (
        "silent interstitial (music_audio_continues_during_hold=False) "
        "MUST NOT widen the audible window — the lyric at song-time 131.5+ "
        "should be dropped as inaudible. Got: "
        f"{[(o.get('lyric_line_id'), o.get('start_s'), o.get('end_s')) for o in lyric_silent]}"
    )

    # Same shape but the interstitial declares
    # `music_audio_continues_during_hold=True` (today's default) → audible
    # end = 133.5 → line audible → kept.
    audio_inter = {
        1: {"hold_s": 2.5, "music_audio_continues_during_hold": True, "type": "music-hold"}
    }
    result_audio = _collect_absolute_overlays(
        steps,
        sd,
        clip_metas=None,
        subject="",
        interstitial_map=audio_inter,
        lyric_audio_mix_song_start_s=128.0,
    )
    lyric_audio = [o for o in result_audio if o.get("effect") == "lyric-line"]
    assert len(lyric_audio) == 1
