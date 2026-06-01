"""Unit tests for LyricStyleSelectorAgent.parse() — id validation + fallback."""

from __future__ import annotations

import json

from app.agents._schemas.music_labels import MusicLabels
from app.agents.lyric_style_selector import (
    LyricStyleSelectorAgent,
    LyricStyleSelectorInput,
)


def _labels() -> MusicLabels:
    return MusicLabels(
        label_version="2026-05-15",
        genre="pop",
        vibe_tags=["energetic"],
        energy="high",
        pacing="fast",
        mood="upbeat",
        ideal_content_profile="dance clips",
        copy_tone="high_energy",
        color_grade="vibrant",
        transition_style="beat_pulse",
    )


def _agent() -> LyricStyleSelectorAgent:
    return LyricStyleSelectorAgent(model_client=None)  # type: ignore[arg-type]


def _input() -> LyricStyleSelectorInput:
    return LyricStyleSelectorInput(labels=_labels(), title="Test Song")


def test_valid_id_kept() -> None:
    raw = json.dumps({"style_set_id": "lyric_karaoke_bold", "rationale": "energetic pop"})
    out = _agent().parse(raw, _input())
    assert out.style_set_id == "lyric_karaoke_bold"


def test_hallucinated_id_falls_back_to_default() -> None:
    raw = json.dumps({"style_set_id": "totally-made-up", "rationale": "x"})
    out = _agent().parse(raw, _input())
    assert out.style_set_id == "default"


def test_empty_id_falls_back() -> None:
    out = _agent().parse(json.dumps({"style_set_id": ""}), _input())
    assert out.style_set_id == "default"


def test_invalid_json_raises() -> None:
    import pytest

    from app.agents._runtime import SchemaError

    with pytest.raises(SchemaError):
        _agent().parse("not json", _input())


def test_render_prompt_lists_only_music_sets() -> None:
    prompt = _agent().render_prompt(_input())
    assert "lyric_karaoke_bold" in prompt
    # Agentic-only set must not be offered.
    assert "travel_editorial" not in prompt
