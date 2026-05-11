"""Unit tests for runners/llm_judge.py — mocks the Anthropic client."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from .runners.llm_judge import (
    DEFAULT_PASS_THRESHOLD,
    JudgeError,
    LLMJudge,
    _parse_judge_json,
)


@dataclass
class _FakeBlock:
    text: str


@dataclass
class _FakeResponse:
    content: list


class _FakeMessages:
    def __init__(self, response_text: str | None = None, raise_exc: Exception | None = None):
        self.response_text = response_text
        self.raise_exc = raise_exc
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.raise_exc:
            raise self.raise_exc
        return _FakeResponse(content=[_FakeBlock(text=self.response_text or "")])


class _FakeClient:
    def __init__(self, response_text: str | None = None, raise_exc: Exception | None = None):
        self.messages = _FakeMessages(response_text, raise_exc)


def _write_rubric(tmp_path: Path, threshold: float = 3.5) -> Path:
    p = tmp_path / "rubric.md"
    p.write_text(
        f"# rubric\n\n1. **dim_a** — explanation\n2. **dim_b** — explanation\n\n"
        f"Pass threshold: avg ≥ {threshold}\n"
    )
    return p


def test_parse_judge_json_strips_fences():
    raw = '```json\n{"scores": {"a": 4, "b": 3}, "reasoning": "ok"}\n```'
    scores, reasoning = _parse_judge_json(raw)
    assert scores == {"a": 4.0, "b": 3.0}
    assert reasoning == "ok"


def test_parse_judge_json_extracts_object_from_prose():
    raw = 'Here is my evaluation: {"scores": {"a": 5}} done.'
    scores, _ = _parse_judge_json(raw)
    assert scores == {"a": 5.0}


def test_parse_judge_json_raises_on_missing_object():
    with pytest.raises(JudgeError):
        _parse_judge_json("no json here")


def test_parse_judge_json_raises_on_invalid_json():
    with pytest.raises(JudgeError):
        _parse_judge_json("{this is not json}")


def test_judge_score_passes(tmp_path: Path):
    rubric = _write_rubric(tmp_path, threshold=3.5)
    client = _FakeClient(
        response_text=json.dumps({"scores": {"dim_a": 5, "dim_b": 4}, "reasoning": "great"})
    )
    judge = LLMJudge(rubric, client=client)

    result = judge.score(
        agent_name="nova.test", agent_input={"x": 1}, agent_output={"y": 2}
    )

    assert result.passed
    assert result.avg == 4.5
    assert result.threshold == 3.5
    assert result.scores == {"dim_a": 5.0, "dim_b": 4.0}
    assert result.reasoning == "great"


def test_judge_score_fails_below_threshold(tmp_path: Path):
    rubric = _write_rubric(tmp_path, threshold=3.5)
    client = _FakeClient(
        response_text=json.dumps({"scores": {"dim_a": 2, "dim_b": 3}, "reasoning": "weak"})
    )
    judge = LLMJudge(rubric, client=client)
    result = judge.score(agent_name="nova.test", agent_input={}, agent_output={})
    assert not result.passed


def test_judge_uses_prompt_caching(tmp_path: Path):
    rubric = _write_rubric(tmp_path)
    client = _FakeClient(response_text=json.dumps({"scores": {"a": 4}}))
    judge = LLMJudge(rubric, client=client)
    judge.score(agent_name="nova.test", agent_input={}, agent_output={})

    # Both system blocks must declare cache_control so the rubric is cacheable
    # across repeated judge calls in one eval run.
    call = client.messages.calls[0]
    system = call["system"]
    assert any("cache_control" in block for block in system)


def test_judge_call_failure_raises(tmp_path: Path):
    rubric = _write_rubric(tmp_path)
    client = _FakeClient(raise_exc=RuntimeError("api timeout"))
    judge = LLMJudge(rubric, client=client)
    with pytest.raises(JudgeError, match="judge call failed"):
        judge.score(agent_name="nova.test", agent_input={}, agent_output={})


def test_judge_missing_rubric_raises(tmp_path: Path):
    judge = LLMJudge(tmp_path / "nope.md", client=_FakeClient(""))
    with pytest.raises(JudgeError, match="rubric not found"):
        judge.score(agent_name="nova.test", agent_input={}, agent_output={})


def test_judge_no_scores_raises(tmp_path: Path):
    rubric = _write_rubric(tmp_path)
    client = _FakeClient(response_text=json.dumps({"scores": {}}))
    judge = LLMJudge(rubric, client=client)
    with pytest.raises(JudgeError, match="no scores"):
        judge.score(agent_name="nova.test", agent_input={}, agent_output={})


def test_judge_threshold_default_when_missing(tmp_path: Path):
    p = tmp_path / "nothreshold.md"
    p.write_text("# rubric\n\nNo threshold here.\n")
    client = _FakeClient(response_text=json.dumps({"scores": {"a": 4}}))
    judge = LLMJudge(p, client=client)
    result = judge.score(agent_name="nova.test", agent_input={}, agent_output={})
    assert result.threshold == DEFAULT_PASS_THRESHOLD
