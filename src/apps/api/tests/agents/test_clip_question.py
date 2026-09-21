"""Unit tests for nova.video.clip_question parse() invariants."""

from __future__ import annotations

import json

import pytest

from app.agents._runtime import SchemaError
from app.agents.clip_question import ClipQuestionAgent, ClipQuestionInput


def _input() -> ClipQuestionInput:
    return ClipQuestionInput(
        file_uri="files/abc123",
        file_mime="video/mp4",
        question="What sport is being played?",
    )


def _agent() -> ClipQuestionAgent:
    return ClipQuestionAgent(None)  # type: ignore[arg-type]


def test_media_uri_and_mime_come_from_input() -> None:
    agent = _agent()
    inp = _input()
    assert agent.media_uri(inp) == "files/abc123"
    assert agent.media_mime(inp) == "video/mp4"


def test_parse_confident_answer() -> None:
    raw = json.dumps({"answer": "Soccer", "confidence": 0.92, "evidence": "players kicking a ball"})
    out = _agent().parse(raw, _input())
    assert out.answer == "Soccer"
    assert out.confidence == 0.92
    assert out.evidence == "players kicking a ball"
    assert not out.is_unknown()


def test_parse_unknown_normalizes_to_empty_answer_and_zero_confidence() -> None:
    raw = json.dumps({"answer": "unknown", "confidence": 0.4, "evidence": ""})
    out = _agent().parse(raw, _input())
    assert out.answer == ""
    assert out.confidence == 0.0
    assert out.is_unknown()


def test_parse_empty_answer_string_is_unknown() -> None:
    raw = json.dumps({"answer": "", "confidence": 0.0, "evidence": ""})
    out = _agent().parse(raw, _input())
    assert out.is_unknown()


def test_parse_caps_answer_at_three_words() -> None:
    raw = json.dumps({"answer": "a very long answer indeed", "confidence": 0.7, "evidence": ""})
    out = _agent().parse(raw, _input())
    assert len(out.answer.split()) <= 3


def test_parse_clamps_confidence() -> None:
    raw = json.dumps({"answer": "Volleyball", "confidence": 5.0, "evidence": "net and ball"})
    out = _agent().parse(raw, _input())
    assert out.confidence == 1.0


def test_parse_raises_on_malformed_json() -> None:
    with pytest.raises(SchemaError):
        _agent().parse("not json", _input())


def test_parse_raises_on_non_object_json() -> None:
    with pytest.raises(SchemaError):
        _agent().parse("[1, 2, 3]", _input())
