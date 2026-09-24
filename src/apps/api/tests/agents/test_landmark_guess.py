"""Unit tests for nova.video.landmark_guess (KRI-189): parse() + prompt invariants."""

from __future__ import annotations

import json

import pytest

from app.agents._runtime import SchemaError
from app.agents.landmark_guess import (
    LandmarkGuessAgent,
    LandmarkGuessInput,
    normalize_landmark_name,
)


def _input(**overrides: object) -> LandmarkGuessInput:
    base: dict[str, object] = {
        "file_uri": "files/abc123",
        "file_mime": "video/mp4",
        "place": "Sarıyer, İstanbul, Türkiye",
        "lat": 41.09,
        "lon": 29.06,
    }
    base.update(overrides)
    return LandmarkGuessInput(**base)  # type: ignore[arg-type]


def _agent() -> LandmarkGuessAgent:
    return LandmarkGuessAgent(None)  # type: ignore[arg-type]


def test_prompt_carries_place_and_coarse_coordinates() -> None:
    prompt = _agent().render_prompt(_input())
    assert "Sarıyer, İstanbul, Türkiye" in prompt
    assert "41.09, 29.06" in prompt


def test_prompt_without_place_says_not_available() -> None:
    prompt = _agent().render_prompt(_input(place="", lat=None, lon=None))
    assert prompt.count("(not available)") == 2


def test_parse_named_landmark_keeps_native_spelling() -> None:
    raw = json.dumps(
        {"name": "Rumeli Hisarı", "confidence": 0.82, "evidence": "fortress towers"},
        ensure_ascii=False,
    )
    out = _agent().parse(raw, _input())
    assert out.name == "Rumeli Hisarı"
    assert out.confidence == 0.82
    assert not out.is_unknown()


def test_parse_unknown_folds_to_empty_name_and_zero_confidence() -> None:
    out = _agent().parse(json.dumps({"name": "unknown", "confidence": 0.6}), _input())
    assert out.is_unknown()
    assert out.confidence == 0.0


def test_parse_caps_name_at_six_words_and_clamps_confidence() -> None:
    raw = json.dumps({"name": "a b c d e f g h", "confidence": 9})
    out = _agent().parse(raw, _input())
    assert len(out.name.split()) == 6
    assert out.confidence == 1.0


@pytest.mark.parametrize("raw", ["not json", "[1, 2]"])
def test_parse_rejects_non_object_json(raw: str) -> None:
    with pytest.raises(SchemaError):
        _agent().parse(raw, _input())


def test_normalize_handles_non_strings() -> None:
    assert normalize_landmark_name(None) == ""
    assert normalize_landmark_name("  N/A. ") == ""


@pytest.mark.parametrize(
    "answer", ["Bilinmiyor", "BİLİNMİYOR.", "bilinmeyen", "Tanımlanamadı", "belirsiz"]
)
def test_turkish_unknown_answers_fold_to_no_landmark(answer: str) -> None:
    assert normalize_landmark_name(answer) == ""
    out = _agent().parse(json.dumps({"name": answer, "confidence": 0.7}), _input())
    assert out.is_unknown()
