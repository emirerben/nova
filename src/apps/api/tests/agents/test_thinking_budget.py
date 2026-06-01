"""GeminiClient.invoke thinking-budget plumbing.

The gemini-2.5 default dynamic thinking burned ~6.6k thought-tokens / ~30s on the
music_matcher call (measured A/B on the real 34-track prod input). A per-agent
`thinking_budget` on AgentSpec caps that. These tests lock the wiring: the budget
reaches `GenerateContentConfig.thinking_config` for gemini-2.5 models, is omitted
when unset, and is omitted for non-2.5 models (where it would be meaningless).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.agents._model_client import GeminiClient
from app.agents.music_matcher import MusicMatcherAgent


class _CapturingModels:
    def __init__(self) -> None:
        self.captured_config: Any = None

    def generate_content(self, *, model: str, contents: Any, config: Any):  # noqa: ARG002
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
    # Stop the model-rewrite from swapping our model name out.
    monkeypatch.setattr(
        "app.pipeline.agents.gemini_analyzer.settings",
        SimpleNamespace(gemini_model=None),
        raising=False,
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
