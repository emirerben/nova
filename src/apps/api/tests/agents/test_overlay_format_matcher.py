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


def test_prompt_en_branch_no_turkish_hint():
    text = _agent().render_prompt(_input())  # default language="en"
    assert "Turkish" not in text
    assert "agglutinative" not in text


def test_prompt_tr_branch_includes_agglutinative_hint():
    # The Turkish hint must reach the prompt — without it the matcher will pick
    # karaoke-line form for any high-energy Turkish phrase, even though Turkish
    # phrasings expand 30-50% over English and overflow snappy reveals.
    tr_input = OverlayFormatMatcherInput(
        clip_set_summary="hair tutorial",
        hero_clip=ClipSummary(clip_id="c1", duration_s=4.0, subject="hair", hook_score=8.0),
        language="tr",
    )
    text = _agent().render_prompt(tr_input)
    assert "TURKISH" in text
    assert "agglutinative" in text


def test_language_defaults_to_en():
    inp = OverlayFormatMatcherInput(
        clip_set_summary="x",
        hero_clip=ClipSummary(clip_id="c1", duration_s=4.0, subject="x", hook_score=5.0),
    )
    assert inp.language == "en"
