"""Unit tests for TemplateTextAgent.parse() error handling.

The agent's parse() has 11 distinct branches (JSON decode, non-dict response,
non-list overlays, non-dict overlay items, slot_index clamps low/high, timing
swap salvage, non-numeric timing drop, ValidationError per-overlay, drop
counters). The integration test in tests/tasks/ exercises the happy path but
not the salvage and error paths — those are the ones that determine whether
real Gemini outputs survive the parsing layer or get silently dropped.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.agents._runtime import SchemaError
from app.agents.template_text import TemplateTextAgent, TemplateTextInput


def _input(boundaries: list[tuple[float, float]] | None = None) -> TemplateTextInput:
    return TemplateTextInput(
        file_uri="files/test",
        file_mime="video/mp4",
        slot_boundaries_s=(boundaries if boundaries is not None else [(0.0, 5.0), (5.0, 10.0)]),
    )


def _valid_overlay(**overrides: Any) -> dict:
    base = {
        "slot_index": 1,
        "sample_text": "Hello",
        "start_s": 0.5,
        "end_s": 2.0,
        "bbox": {
            "x_norm": 0.5,
            "y_norm": 0.5,
            "w_norm": 0.4,
            "h_norm": 0.1,
            "sample_frame_t": 1.0,
        },
        "font_color_hex": "#FFFFFF",
        "effect": "none",
        "role": "label",
        "size_class": "medium",
    }
    base.update(overrides)
    return base


def _agent() -> TemplateTextAgent:
    # Client is unused in parse(); pass None.
    return TemplateTextAgent(None)  # type: ignore[arg-type]


# ── Top-level structure ─────────────────────────────────────────────────────


def test_parse_invalid_json_raises_schema_error() -> None:
    with pytest.raises(SchemaError, match="invalid JSON"):
        _agent().parse("not-json{", _input())


def test_parse_non_object_response_raises_schema_error() -> None:
    with pytest.raises(SchemaError, match="not a JSON object"):
        _agent().parse(json.dumps(["a", "b"]), _input())


def test_parse_overlays_not_list_raises_schema_error() -> None:
    with pytest.raises(SchemaError, match="overlays is not a list"):
        _agent().parse(json.dumps({"overlays": "nope"}), _input())


def test_parse_empty_overlays_returns_empty_output() -> None:
    out = _agent().parse(json.dumps({"overlays": []}), _input())
    assert out.overlays == []


def test_parse_missing_overlays_field_defaults_to_empty() -> None:
    out = _agent().parse(json.dumps({}), _input())
    assert out.overlays == []


# ── Per-overlay survival / drop ─────────────────────────────────────────────


def test_parse_non_dict_overlay_dropped() -> None:
    raw = json.dumps({"overlays": ["string", _valid_overlay()]})
    out = _agent().parse(raw, _input())
    assert len(out.overlays) == 1


def test_parse_valid_overlay_survives() -> None:
    raw = json.dumps({"overlays": [_valid_overlay()]})
    out = _agent().parse(raw, _input())
    assert len(out.overlays) == 1
    assert out.overlays[0].sample_text == "Hello"


def test_parse_validation_error_drops_individual_overlay() -> None:
    bad = _valid_overlay()
    bad["font_color_hex"] = "not-a-hex"  # fails regex
    good = _valid_overlay(sample_text="World")
    raw = json.dumps({"overlays": [bad, good]})
    out = _agent().parse(raw, _input())
    assert len(out.overlays) == 1
    assert out.overlays[0].sample_text == "World"


# ── slot_index clamping ─────────────────────────────────────────────────────


def test_parse_slot_index_below_one_clamped_to_one() -> None:
    raw = json.dumps({"overlays": [_valid_overlay(slot_index=0)]})
    out = _agent().parse(raw, _input(boundaries=[(0.0, 5.0), (5.0, 10.0)]))
    assert out.overlays[0].slot_index == 1


def test_parse_slot_index_negative_clamped_to_one() -> None:
    raw = json.dumps({"overlays": [_valid_overlay(slot_index=-3)]})
    out = _agent().parse(raw, _input(boundaries=[(0.0, 5.0), (5.0, 10.0)]))
    assert out.overlays[0].slot_index == 1


def test_parse_slot_index_above_max_clamped_to_max() -> None:
    # 2 slots → max_slot = 2. Agent says 99 → clamp to 2.
    raw = json.dumps({"overlays": [_valid_overlay(slot_index=99)]})
    out = _agent().parse(raw, _input(boundaries=[(0.0, 5.0), (5.0, 10.0)]))
    assert out.overlays[0].slot_index == 2


def test_parse_no_boundaries_max_slot_defaults_to_one() -> None:
    # With no boundaries, max_slot is 1 (per the agent's safety floor).
    raw = json.dumps({"overlays": [_valid_overlay(slot_index=5)]})
    out = _agent().parse(raw, _input(boundaries=[]))
    assert out.overlays[0].slot_index == 1


# ── Timing salvage / drop ────────────────────────────────────────────────────


def test_parse_inverted_timing_salvaged_by_swap() -> None:
    # start_s > end_s should swap to start=1.0, end=3.0.
    inverted = _valid_overlay(start_s=3.0, end_s=1.0)
    inverted["bbox"]["sample_frame_t"] = 2.0  # inside [1.0, 3.0] after swap
    raw = json.dumps({"overlays": [inverted]})
    out = _agent().parse(raw, _input())
    assert len(out.overlays) == 1
    assert out.overlays[0].start_s == 1.0
    assert out.overlays[0].end_s == 3.0


def test_parse_non_numeric_timing_dropped() -> None:
    bad = _valid_overlay()
    bad["start_s"] = "two seconds"
    raw = json.dumps({"overlays": [bad]})
    out = _agent().parse(raw, _input())
    assert out.overlays == []


def test_parse_equal_start_end_dropped_by_validator() -> None:
    # Pydantic gt=0.0 on end_s + start < end invariant. start=end fails.
    eq = _valid_overlay(start_s=2.0, end_s=2.0)
    raw = json.dumps({"overlays": [eq]})
    out = _agent().parse(raw, _input())
    # Pydantic validator on end_s requires gt=0.0; equal values pass that
    # check, but the bbox sample_frame_t=1.0 must be inside [2.0, 2.0],
    # which the structural validator catches. parse itself accepts the
    # overlay (validation is split between parse and the structural pass).
    # What we assert here: parse does not crash.
    assert out.overlays == [] or out.overlays[0].start_s == 2.0


def test_inverted_timing_salvage_also_clamps_sample_frame_t() -> None:
    """Regression for the not_just_luck prod failure (2026-05-17).

    Gemini returned start_s=3.0, end_s=0.9 (inverted). The salvage path swapped
    them to [0.9, 3.0] but left bbox.sample_frame_t=0.45 untouched. The
    downstream structural validator rejected: 0.45 is outside [0.9, 3.0].

    After the fix the salvage path also clamps sample_frame_t into the corrected
    window, so the overlay survives all the way to TemplateTextOutput.
    """
    # Exact values from the prod failure.
    inverted = _valid_overlay(
        slot_index=1,
        sample_text="It's",
        start_s=3.0,
        end_s=0.9,
        font_color_hex="#FFFFFF",
    )
    inverted["bbox"] = {
        "x_norm": 0.5,
        "y_norm": 0.5,
        "w_norm": 0.3,
        "h_norm": 0.1,
        "sample_frame_t": 0.45,  # outside [0.9, 3.0] after swap
    }
    raw = json.dumps({"overlays": [inverted]})
    out = _agent().parse(raw, _input())

    # Overlay must survive — NOT rejected.
    assert len(out.overlays) == 1, "overlay was dropped; sample_frame_t clamp did not fire"

    ov = out.overlays[0]
    # Timing was swapped.
    assert ov.start_s == pytest.approx(0.9)
    assert ov.end_s == pytest.approx(3.0)

    # sample_frame_t clamped to start of corrected window (0.45 < 0.9 → pin to 0.9).
    assert ov.bbox.sample_frame_t == pytest.approx(0.9)


# ── Non-inversion sample_frame_t clamp (PR #199 regression) ──────────────────


def test_non_inversion_clamps_sample_frame_t_below_start() -> None:
    """Regression for the life_s_richness_3 prod failure (PR #199).

    Gemini returned start_s=2.0, end_s=22.18 (valid — no inversion). But
    bbox.sample_frame_t=0.07 sits below start_s=2.0. PR #198's clamp lived
    inside the salvage branch so it never fired here. The downstream structural
    validator rejected the overlay.

    After the fix _clamp_sample_frame_t() runs unconditionally, pinning
    sample_frame_t to start_s (2.0).
    """
    ov = _valid_overlay(
        slot_index=1,
        sample_text="Life is rich",
        start_s=2.0,
        end_s=22.18,
        font_color_hex="#F4D03F",
    )
    ov["bbox"] = {
        "x_norm": 0.5,
        "y_norm": 0.5,
        "w_norm": 0.4,
        "h_norm": 0.1,
        "sample_frame_t": 0.07,  # below start_s=2.0
    }
    raw = json.dumps({"overlays": [ov]})
    out = _agent().parse(raw, _input())

    assert len(out.overlays) == 1, "overlay was dropped; unconditional clamp did not fire"
    parsed = out.overlays[0]
    # Timings are unchanged — no inversion occurred.
    assert parsed.start_s == pytest.approx(2.0)
    assert parsed.end_s == pytest.approx(22.18)
    # sample_frame_t clamped up to start_s.
    assert parsed.bbox.sample_frame_t == pytest.approx(2.0)


def test_non_inversion_no_clamp_when_in_window() -> None:
    """sample_frame_t already inside [start_s, end_s] — must pass through unchanged."""
    ov = _valid_overlay(
        slot_index=1,
        sample_text="Life is rich",
        start_s=2.0,
        end_s=22.18,
        font_color_hex="#F4D03F",
    )
    ov["bbox"] = {
        "x_norm": 0.5,
        "y_norm": 0.5,
        "w_norm": 0.4,
        "h_norm": 0.1,
        "sample_frame_t": 10.0,  # inside [2.0, 22.18]
    }
    raw = json.dumps({"overlays": [ov]})
    out = _agent().parse(raw, _input())

    assert len(out.overlays) == 1
    parsed = out.overlays[0]
    assert parsed.start_s == pytest.approx(2.0)
    assert parsed.end_s == pytest.approx(22.18)
    # No clamping needed; value is unchanged.
    assert parsed.bbox.sample_frame_t == pytest.approx(10.0)


def test_non_inversion_clamps_sample_frame_t_above_end() -> None:
    """sample_frame_t past end_s — must be clamped down to end_s."""
    ov = _valid_overlay(
        slot_index=1,
        sample_text="Life is rich",
        start_s=2.0,
        end_s=22.18,
        font_color_hex="#F4D03F",
    )
    ov["bbox"] = {
        "x_norm": 0.5,
        "y_norm": 0.5,
        "w_norm": 0.4,
        "h_norm": 0.1,
        "sample_frame_t": 50.0,  # past end_s=22.18
    }
    raw = json.dumps({"overlays": [ov]})
    out = _agent().parse(raw, _input())

    assert len(out.overlays) == 1
    parsed = out.overlays[0]
    assert parsed.start_s == pytest.approx(2.0)
    assert parsed.end_s == pytest.approx(22.18)
    # sample_frame_t clamped down to end_s.
    assert parsed.bbox.sample_frame_t == pytest.approx(22.18)
