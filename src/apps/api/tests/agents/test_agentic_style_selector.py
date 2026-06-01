"""Unit tests for AgenticStyleSelectorAgent.parse() — id validation + fallback."""

from __future__ import annotations

import json

import pytest

from app.agents._runtime import SchemaError
from app.agents.agentic_style_selector import (
    AgenticStyleSelectorAgent,
    AgenticStyleSelectorInput,
)


def _agent() -> AgenticStyleSelectorAgent:
    return AgenticStyleSelectorAgent(model_client=None)  # type: ignore[arg-type]


def _input() -> AgenticStyleSelectorInput:
    return AgenticStyleSelectorInput(
        overlay_texts=["this is what I call:", "being rich in life"],
        template_theme="rich in life",
    )


def test_valid_id_kept() -> None:
    out = _agent().parse(json.dumps({"style_set_id": "travel_editorial"}), _input())
    assert out.style_set_id == "travel_editorial"


def test_hallucinated_id_falls_back_to_default() -> None:
    out = _agent().parse(json.dumps({"style_set_id": "made-up"}), _input())
    assert out.style_set_id == "default"


def test_music_only_set_rejected_for_agentic() -> None:
    # lyric_karaoke_bold is music-only; not eligible for agentic templates.
    out = _agent().parse(json.dumps({"style_set_id": "lyric_karaoke_bold"}), _input())
    assert out.style_set_id == "default"


def test_empty_falls_back() -> None:
    out = _agent().parse(json.dumps({"style_set_id": ""}), _input())
    assert out.style_set_id == "default"


def test_invalid_json_raises() -> None:
    with pytest.raises(SchemaError):
        _agent().parse("not json", _input())


def test_render_prompt_lists_agentic_sets_only() -> None:
    prompt = _agent().render_prompt(_input())
    assert "travel_editorial" in prompt
    assert "lyric_karaoke_bold" not in prompt  # music-only
    assert "being rich in life" in prompt  # overlay text echoed
