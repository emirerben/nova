"""Shared pytest fixtures + CLI options for the agent-eval suite.

Modes (selected via CLI flags + env):

  Default                           — replay-mode, structural-only, no network.
  --with-judge                      — adds Claude-Sonnet judge call (needs ANTHROPIC_API_KEY).
  --eval-mode=live                  — bypass cassettes, hit real Gemini (needs GEMINI_API_KEY).
  --shadow                          — run candidate prompt alongside prod prompt, log delta.

Set NOVA_EVAL_MODE=live as an alternative to --eval-mode=live.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from .runners.eval_runner import RUBRIC_ROOT, rubric_path_for
from .runners.llm_judge import LLMJudge


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--with-judge",
        action="store_true",
        default=False,
        help="Run LLM-as-judge scoring (needs ANTHROPIC_API_KEY).",
    )
    parser.addoption(
        "--shadow",
        action="store_true",
        default=False,
        help="Run candidate prompt alongside prod (live mode only).",
    )
    parser.addoption(
        "--eval-mode",
        action="store",
        default=None,
        choices=["replay", "live"],
        help="replay (default, uses cassettes) | live (calls real Gemini).",
    )


@pytest.fixture(scope="session")
def eval_mode(request: pytest.FixtureRequest) -> str:
    cli_mode = request.config.getoption("--eval-mode")
    if cli_mode:
        return cli_mode
    return os.environ.get("NOVA_EVAL_MODE", "replay")


@pytest.fixture(scope="session")
def with_judge(request: pytest.FixtureRequest) -> bool:
    return bool(request.config.getoption("--with-judge"))


@pytest.fixture(scope="session")
def shadow_mode(request: pytest.FixtureRequest) -> bool:
    return bool(request.config.getoption("--shadow"))


@pytest.fixture(scope="session")
def rubric_root() -> Path:
    return RUBRIC_ROOT


@pytest.fixture(scope="session")
def judge_for(with_judge: bool, rubric_root: Path):
    """Returns a callable: agent_name -> LLMJudge | None.

    Cached per-agent so the rubric is loaded once and the Anthropic client is
    reused across calls (which lets prompt caching kick in for the rubric block).
    """
    if not with_judge:
        return lambda _agent: None

    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("--with-judge requires ANTHROPIC_API_KEY")

    cache: dict[str, LLMJudge] = {}

    def _factory(agent_name: str) -> LLMJudge:
        if agent_name not in cache:
            cache[agent_name] = LLMJudge(rubric_path_for(agent_name, rubric_root))
        return cache[agent_name]

    return _factory


@pytest.fixture(scope="session")
def live_model_client(eval_mode: str):
    """Build the production ModelDispatcher for --eval-mode=live, else None."""
    if eval_mode != "live":
        return None
    if not os.environ.get("GEMINI_API_KEY"):
        pytest.skip("--eval-mode=live requires GEMINI_API_KEY")
    from app.agents._model_client import default_client

    return default_client()
