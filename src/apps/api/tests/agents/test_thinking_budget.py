"""GeminiClient.invoke thinking-budget plumbing.

The gemini-2.5 default dynamic thinking burned ~6.6k thought-tokens / ~30s on the
music_matcher call (measured A/B on the real 34-track prod input). Gemini 3 adds
named thinking levels. These tests lock both configurations and ensure the
per-agent model declaration reaches the SDK unchanged.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.agents._model_client import GeminiClient
from app.agents._runtime import (
    ModelClient,
    ModelInvocation,
    ProviderOutcomeUnknownError,
    TerminalError,
    TerminalSchemaError,
)
from app.agents.music_matcher import MusicMatcherAgent
from tests.agents.conftest import max_tokens_response


class _CapturingModels:
    def __init__(self) -> None:
        self.captured_config: Any = None
        self.captured_model: str | None = None

    def generate_content(self, *, model: str, contents: Any, config: Any):  # noqa: ARG002
        self.captured_model = model
        self.captured_config = config
        return SimpleNamespace(text='{"ranked": []}', usage_metadata=None)


class _FakeClient:
    def __init__(self) -> None:
        self.models = _CapturingModels()


@pytest.fixture
def capturing_client(monkeypatch) -> _CapturingModels:
    fake = _FakeClient()
    # _get() delegates to gemini_analyzer._get_client; patch that.
    monkeypatch.setattr(
        "app.pipeline.agents.gemini_analyzer._get_client", lambda: fake, raising=True
    )
    return fake.models


def _budget(config: Any) -> int | None:
    tc = getattr(config, "thinking_config", None)
    return getattr(tc, "thinking_budget", None) if tc is not None else None


def test_thinking_budget_reaches_config_for_gemini_2_5(capturing_client):
    GeminiClient().invoke(model="gemini-2.5-flash", prompt="hi", thinking_budget=256)
    assert _budget(capturing_client.captured_config) == 256


def test_no_thinking_config_when_budget_unset(capturing_client):
    GeminiClient().invoke(model="gemini-2.5-flash", prompt="hi")
    assert getattr(capturing_client.captured_config, "thinking_config", None) is None


def test_thinking_budget_ignored_for_non_2_5_model(capturing_client):
    # The param is meaningless on non-2.5 SKUs; don't attach it.
    GeminiClient().invoke(model="gemini-1.5-flash", prompt="hi", thinking_budget=256)
    assert getattr(capturing_client.captured_config, "thinking_config", None) is None


def test_gemini_3_thinking_level_and_declared_model_reach_sdk(capturing_client):
    GeminiClient().invoke(
        model="gemini-3.1-pro-preview",
        prompt="hi",
        thinking_level="high",
    )

    assert capturing_client.captured_model == "gemini-3.1-pro-preview"
    thinking = capturing_client.captured_config.thinking_config
    assert str(thinking.thinking_level).endswith("HIGH")


_KRI178_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures/agent_evals/main_creator/kri178_talking_reaction_beats.json"
)
# `nova.creator.main` latency over its 16 prod runs on 2026-09-23/24, fitted as
# first-token time + time per generated (thinking + answer) token. The
# first-token term includes the worst residual (+2.0 s).
_FIRST_TOKEN_S = 6.7
_S_PER_TOKEN = 0.00573


class _SharedBudgetGemini(ModelClient):
    """Gemini 3 counts thinking against `max_output_tokens`: the visible answer
    gets only what thinking leaves, and generation time grows with both."""

    def __init__(self, *, answer: str, thinking_tokens: int, answer_tokens: int) -> None:
        self.answer = answer
        self.thinking_tokens = thinking_tokens
        self.answer_tokens = answer_tokens
        self.calls: list[dict[str, Any]] = []

    def invoke(
        self,
        *,
        max_output_tokens: int | None = None,
        timeout_s: float = 30.0,
        **_: Any,
    ) -> ModelInvocation:
        self.calls.append({"max_output_tokens": max_output_tokens, "timeout_s": timeout_s})
        wanted = self.thinking_tokens + self.answer_tokens
        budget = max_output_tokens or 0
        if _FIRST_TOKEN_S + min(wanted, budget) * _S_PER_TOKEN > timeout_s:
            raise ProviderOutcomeUnknownError(
                f"gemini provider outcome unknown after {timeout_s:.1f}s"
            )
        if wanted > budget:
            return ModelInvocation(
                raw_text=self.answer[: len(self.answer) // 3],
                raw_response=max_tokens_response(),
                tokens_out=max(0, budget - self.thinking_tokens),
                tokens_thoughts=self.thinking_tokens,
            )
        return ModelInvocation(
            raw_text=self.answer,
            tokens_out=self.answer_tokens,
            tokens_thoughts=self.thinking_tokens,
        )


def test_main_creator_fits_heavy_thinking_plus_a_full_reaction_beat_plan() -> None:
    """Prod 2026-09-24, thread 9b6594a6: on the KRI-172 football prompt the
    Main Creator thought for 3,047 tokens, which left 1,049 of a 4,096 budget
    for a plan whose full answer measured 2,380 tokens (same prompt, 14:56Z run).
    It stopped at MAX_TOKENS and the turn failed. Untruncated, the same run also
    outlasts a 35 s provider timeout, so both limits have to cover it."""
    from app.agents.main_creator import MainCreatorAgent

    fixture = json.loads(_KRI178_FIXTURE.read_text())
    client = _SharedBudgetGemini(
        answer=fixture["raw_text"], thinking_tokens=3_047, answer_tokens=2_380
    )

    output = MainCreatorAgent(client).run(fixture["input"])

    assert len(output.action.strategy.reaction_beats) == 13


def test_main_creator_budget_runs_out_before_its_provider_timeout() -> None:
    """A runaway generation must end as a retryable truncation, never as an
    outcome-unknown timeout that fences the paid call."""
    from app.agents.main_creator import MainCreatorAgent

    runaway_s = _FIRST_TOKEN_S + MainCreatorAgent.max_output_tokens * _S_PER_TOKEN
    assert runaway_s < MainCreatorAgent.spec.timeout_s


def test_runaway_main_creator_call_ends_as_one_truncation_not_an_unknown_outcome() -> None:
    """Behavioral twin of the check above: a call that would think and write
    past its whole output budget stops at MAX_TOKENS inside the provider
    deadline. The run fails once, as a retryable truncation -- never a second
    ~54 s paid call, and never an outcome-unknown fence."""
    from app.agents.main_creator import MainCreatorAgent

    fixture = json.loads(_KRI178_FIXTURE.read_text())
    client = _SharedBudgetGemini(
        answer=fixture["raw_text"], thinking_tokens=7_000, answer_tokens=2_380
    )

    with pytest.raises(TerminalError, match="output truncated") as failure:
        MainCreatorAgent(client).run(fixture["input"])

    assert not isinstance(failure.value, (ProviderOutcomeUnknownError, TerminalSchemaError))
    [call] = client.calls
    # The agent's declared budget and deadline are what reach the provider.
    assert call["max_output_tokens"] == MainCreatorAgent.max_output_tokens
    assert call["timeout_s"] == MainCreatorAgent.spec.timeout_s


def test_per_agent_timeout_is_enforced(capturing_client, monkeypatch):
    from app.agents._runtime import ProviderOutcomeUnknownError

    def slow_generate(**kwargs):  # noqa: ARG001
        time.sleep(0.2)
        return SimpleNamespace(text='{"ranked": []}', usage_metadata=None)

    monkeypatch.setattr(capturing_client, "generate_content", slow_generate)
    # A running SDK call may still reach and bill the provider after our local
    # deadline. It must remain outcome-unknown so the runtime will not overlap
    # it with a retry.
    with pytest.raises(ProviderOutcomeUnknownError, match="unknown after 0.1s"):
        GeminiClient().invoke(
            model="gemini-3.6-flash",
            prompt="hi",
            timeout_s=0.1,
        )


def test_matcher_spec_caps_thinking_budget():
    # The matcher's ~30s thinking tax (vs ~4s capped) is the reason this exists.
    # 256 is honored by flash (prod) and pro (evals), so the eval validates prod.
    assert MusicMatcherAgent.spec.thinking_budget == 256


def test_generative_critical_path_agents_cap_thinking():
    """The generative first-variant critical path is gated by these flash agents.
    Each had the same thinking tax (13-18s default vs ~5s capped, validated on
    real clips with no quality loss). 512 keeps reasoning headroom for the
    extraction/creative steps. Locking the budgets prevents a silent revert to
    the slow default-thinking path.
    """
    from app.agents.agentic_style_selector import AgenticStyleSelectorAgent
    from app.agents.clip_metadata import ClipMetadataAgent
    from app.agents.intro_writer import IntroTextWriterAgent
    from app.agents.overlay_format_matcher import OverlayFormatMatcherAgent

    assert ClipMetadataAgent.spec.thinking_budget == 512
    assert OverlayFormatMatcherAgent.spec.thinking_budget == 512
    assert IntroTextWriterAgent.spec.thinking_budget == 512
    assert AgenticStyleSelectorAgent.spec.thinking_budget == 512
