"""Unit tests for OverlayFormatMatcherAgent.parse() — effect coercion + id filtering.

parse() never touches the model client, so we construct the agent directly and feed it
raw JSON strings.
"""

from __future__ import annotations

import json

import pytest

from app.agents.music_matcher import ClipSummary
from app.agents.overlay_format_matcher import (
    OverlayFormatMatcherAgent,
    OverlayFormatMatcherInput,
)


def _input() -> OverlayFormatMatcherInput:
    return OverlayFormatMatcherInput(
        clip_set_summary="n_clips=3 | avg_energy=7.0",
        hero_clip=ClipSummary(clip_id="c1", duration_s=4.0, subject="dog", hook_score=8.0),
    )


def _agent() -> OverlayFormatMatcherAgent:
    return OverlayFormatMatcherAgent.__new__(OverlayFormatMatcherAgent)


def test_valid_effect_preserved():
    raw = json.dumps({"effect": "karaoke-line", "position": "center", "size_class": "jumbo"})
    out = _agent().parse(raw, _input())
    assert out.effect == "karaoke-line"
    assert out.position == "center"
    assert out.size_class == "jumbo"


def test_unknown_effect_coerced_to_static():
    raw = json.dumps({"effect": "explode-confetti"})
    out = _agent().parse(raw, _input())
    assert out.effect == "static"


def test_unknown_position_size_anchor_coerced_to_defaults():
    raw = json.dumps(
        {
            "effect": "pop-in",
            "position": "diagonal",
            "size_class": "ginormous",
            "text_anchor": "middle",
        }
    )
    out = _agent().parse(raw, _input())
    assert out.position == "center"
    assert out.size_class == "jumbo"
    assert out.text_anchor == "center"


def test_bad_hex_color_falls_back():
    raw = json.dumps({"effect": "static", "text_color": "not-a-color", "highlight_color": "#GGG"})
    out = _agent().parse(raw, _input())
    assert out.text_color == "#FFFFFF"
    assert out.highlight_color == "#FFD24A"


def test_valid_hex_uppercased():
    raw = json.dumps({"effect": "static", "text_color": "#ffd24a"})
    out = _agent().parse(raw, _input())
    assert out.text_color == "#FFD24A"


def test_matched_example_ids_filtered_to_library():
    raw = json.dumps(
        {"effect": "karaoke-line", "matched_example_ids": ["pov-surprise-karaoke-01", "made-up-id"]}
    )
    out = _agent().parse(raw, _input())
    assert out.matched_example_ids == ["pov-surprise-karaoke-01"]


def test_invalid_json_raises():
    from app.agents._runtime import SchemaError

    with pytest.raises(SchemaError):
        _agent().parse("not json", _input())


def test_non_object_raises():
    from app.agents._runtime import SchemaError

    with pytest.raises(SchemaError):
        _agent().parse(json.dumps(["a", "b"]), _input())


def test_prompt_renders_with_library():
    # render_prompt must load the library and substitute all template vars.
    text = _agent().render_prompt(_input())
    assert "karaoke-line" in text
    assert "pov-surprise-karaoke-01" in text
