"""Unit tests for nova.plan.idea_expander."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from app.agents._runtime import RefusalError
from app.agents.idea_expander import IdeaExpanderAgent, IdeaExpanderInput


def _agent() -> IdeaExpanderAgent:
    return IdeaExpanderAgent(MagicMock())


def _input() -> IdeaExpanderInput:
    return IdeaExpanderInput(idea="visit the new coffee shop")


def _payload(filming_guide: list[dict]) -> str:
    return json.dumps(
        {
            "theme": "Coffee shop first visit",
            "filming_suggestion": "Film the entrance and first sip",
            "filming_guide": filming_guide,
            "rationale": "Creates curiosity",
        }
    )


def test_parse_raises_refusal_error_on_empty_filming_guide() -> None:
    with pytest.raises(RefusalError, match="filming_guide must contain 2-4 concrete shots"):
        _agent().parse(_payload([]), _input())


def test_parse_raises_refusal_error_when_blank_what_shots_sanitize_to_empty() -> None:
    with pytest.raises(RefusalError, match="filming_guide must contain 2-4 concrete shots"):
        _agent().parse(
            _payload([{"what": "  ", "how": "close-up", "duration_s": 3}]),
            _input(),
        )


def test_parse_allows_one_shot_for_talking_to_camera() -> None:
    output = _agent().parse(
        _payload(
            [
                {
                    "what": "Creator explains the point",
                    "how": "Eye-level framing",
                    "duration_s": 5,
                }
            ]
        ),
        IdeaExpanderInput(
            idea="why I changed my routine",
            video_type="talking_to_camera",
        ),
    )

    assert len(output.filming_guide) == 1


def test_parse_rejects_one_shot_for_montage() -> None:
    with pytest.raises(RefusalError, match="filming_guide must contain 2-4 concrete shots"):
        _agent().parse(
            _payload([{"what": "First sip", "how": "Close-up", "duration_s": 3}]),
            _input(),
        )


def test_render_prompt_threads_context_type_and_content_mode() -> None:
    prompt = _agent().render_prompt(
        IdeaExpanderInput(
            idea="visit the new coffee shop",
            creator_context="I want people to save it for a rainy weekend.",
            video_type="voiceover",
            content_mode="existing_footage",
        )
    )

    assert "I want people to save it for a rainy weekend." in prompt
    assert "SELECTED VIDEO TYPE (DATA): voiceover" in prompt
    assert "CONTENT MODE (DATA): existing_footage" in prompt
