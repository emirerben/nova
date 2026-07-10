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
