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

from app.agents._model_client import GeminiClient, _collect_gemini_stream
from app.agents._runtime import (
    ModelClient,
    ModelInvocation,
    ProviderOutcomeUnknownError,
    RunContext,
    TerminalError,
    TerminalSchemaError,
)
from app.agents.music_matcher import MusicMatcherAgent
from tests.agents.conftest import max_tokens_response


class _CapturingModels:
    def __init__(self) -> None:
        self.captured_config: Any = None
        self.captured_model: str | None = None
        self.stream_called = False

    def generate_content(self, *, model: str, contents: Any, config: Any):  # noqa: ARG002
        self.captured_model = model
        self.captured_config = config
        return SimpleNamespace(text='{"ranked": []}', usage_metadata=None)

    def generate_content_stream(self, *, model: str, contents: Any, config: Any):  # noqa: ARG002
        self.stream_called = True
        self.captured_model = model
        self.captured_config = config
        yield SimpleNamespace(
            candidates=[
                SimpleNamespace(
                    content=SimpleNamespace(parts=[SimpleNamespace(text="Planning", thought=True)])
                )
            ],
            usage_metadata=None,
        )
        yield SimpleNamespace(
            candidates=[
                SimpleNamespace(
                    content=SimpleNamespace(
                        parts=[SimpleNamespace(text='{"ranked": []}', thought=False)]
                    )
                )
            ],
            usage_metadata=SimpleNamespace(prompt_token_count=5, candidates_token_count=3),
        )


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
    assert not capturing_client.stream_called


def test_opted_in_gemini_call_streams_summaries_and_keeps_json_usage(capturing_client):
    seen: list[str] = []
    invocation = GeminiClient().invoke(
        model="gemini-2.5-flash",
        prompt="hi",
        thinking_budget=256,
        thought_summary_callback=seen.append,
    )
    assert capturing_client.stream_called
    assert capturing_client.captured_config.thinking_config.include_thoughts
    assert seen == ["Planning"]
    assert invocation.raw_text == '{"ranked": []}'
    assert (invocation.tokens_in, invocation.tokens_out) == (5, 3)


def test_gemini_3_stream_preserves_thinking_level(capturing_client):
    GeminiClient().invoke(
        model="gemini-3.1-pro-preview",
        prompt="hi",
        thinking_level="high",
        thought_summary_callback=lambda _: None,
    )
    thinking = capturing_client.captured_config.thinking_config
    assert capturing_client.stream_called
    assert str(thinking.thinking_level).endswith("HIGH")
    assert thinking.include_thoughts


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


def test_stream_assembly_forwards_only_provider_marked_thoughts() -> None:
    """Thought text stays out of the final JSON, including signatures."""
    seen: list[str] = []
    first = SimpleNamespace(
        candidates=[
            SimpleNamespace(
                content=SimpleNamespace(
                    parts=[
                        SimpleNamespace(
                            text="private thought", thought=True, thought_signature="secret"
                        ),
                        SimpleNamespace(text='{"answer":', thought=False),
                    ]
                )
            )
        ],
        usage_metadata=None,
    )
    final_usage = SimpleNamespace(prompt_token_count=12, candidates_token_count=3)
    last = SimpleNamespace(
        candidates=[
            SimpleNamespace(
                content=SimpleNamespace(parts=[SimpleNamespace(text="true}", thought=False)])
            )
        ],
        usage_metadata=final_usage,
        model_version="gemini-test",
    )
    response = _collect_gemini_stream([first, last], seen.append)
    assert seen == ["private thought"]
    assert response.text == '{"answer":true}'
    assert response.usage_metadata is final_usage


def test_stream_assembly_rejects_empty_provider_stream() -> None:
    with pytest.raises(TerminalError, match="stream returned no chunks"):
        _collect_gemini_stream([], lambda _: None)


_KRI178_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures/agent_evals/main_creator/kri178_talking_reaction_beats.json"
)
# `nova.creator.main` latency fitted as first-token time + time per generated
# (thinking + answer) token. The 2026-09-23/24 fit over 16 prod runs (small
# manifests, thinking "low") was 6.7 s + 5.73 ms/token. KRI-542 (2026-10-08):
# four single-call replays of an 18.8k-token manifest at thinking "high" with a
# 16k budget measured (7,083 tok, 56.7 s), (8,724, 66.6 s), (9,586, 75.9 s),
# (13,853, 105.9 s): 5.2 s + 7.27 ms/token. The steeper per-token slope is the
# one the budget/timeout invariant below has to survive.
_FIRST_TOKEN_S = 5.2
_S_PER_TOKEN = 0.00727


class _SharedBudgetGemini(ModelClient):
    """Gemini 3 counts thinking against `max_output_tokens`: the visible answer
    gets only what thinking leaves, and generation time grows with both."""

    def __init__(
        self,
        *,
        answer: str,
        thinking_tokens: int,
        answer_tokens: int,
        thinking_tokens_by_level: dict[str, int] | None = None,
    ) -> None:
        self.answer = answer
        self.thinking_tokens = thinking_tokens
        self.answer_tokens = answer_tokens
        # KRI-542: how much the model thinks at each requested level; the
        # default `thinking_tokens` applies to any level not listed.
        self.thinking_tokens_by_level = thinking_tokens_by_level or {}
        self.calls: list[dict[str, Any]] = []

    def invoke(
        self,
        *,
        max_output_tokens: int | None = None,
        timeout_s: float = 30.0,
        thinking_level: str | None = None,
        **_: Any,
    ) -> ModelInvocation:
        self.calls.append(
            {
                "max_output_tokens": max_output_tokens,
                "timeout_s": timeout_s,
                "thinking_level": thinking_level,
            }
        )
        thinking = self.thinking_tokens_by_level.get(thinking_level or "", self.thinking_tokens)
        wanted = thinking + self.answer_tokens
        budget = max_output_tokens or 0
        if _FIRST_TOKEN_S + min(wanted, budget) * _S_PER_TOKEN > timeout_s:
            raise ProviderOutcomeUnknownError(
                f"gemini provider outcome unknown after {timeout_s:.1f}s"
            )
        if wanted > budget:
            return ModelInvocation(
                raw_text=self.answer[: len(self.answer) // 3],
                raw_response=max_tokens_response(),
                tokens_out=max(0, budget - thinking),
                tokens_thoughts=thinking,
            )
        return ModelInvocation(
            raw_text=self.answer,
            tokens_out=self.answer_tokens,
            tokens_thoughts=thinking,
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


def test_agent_marks_only_a_parsed_thought_attempt_successful(monkeypatch) -> None:
    from app.agents.main_creator import MainCreatorAgent

    class Marker:
        attempts = 0
        successes = 0

        def begin_attempt(self):
            self.attempts += 1
            return lambda _: None

        def mark_model_success(self):
            self.successes += 1

    fixture = json.loads(_KRI178_FIXTURE.read_text())
    marker = Marker()
    client = _SharedBudgetGemini(
        answer=fixture["raw_text"], thinking_tokens=100, answer_tokens=2_380
    )
    monkeypatch.setattr("app.agents._runtime.time.monotonic", lambda: 100.0)
    MainCreatorAgent(client).run(
        fixture["input"],
        ctx=RunContext(
            thought_summary_callback=marker,
            deadline_monotonic=140.0,
            timeout_override_s=120.0,
        ),
    )
    assert (marker.attempts, marker.successes) == (1, 1)
    assert client.calls[0]["timeout_s"] == 40.0

    failed_marker = Marker()
    truncated = _SharedBudgetGemini(
        answer=fixture["raw_text"],
        thinking_tokens=MainCreatorAgent.max_output_tokens,
        answer_tokens=2_380,
    )
    with pytest.raises(TerminalError, match="output truncated"):
        MainCreatorAgent(truncated).run(
            fixture["input"], ctx=RunContext(thought_summary_callback=failed_marker)
        )
    assert (failed_marker.attempts, failed_marker.successes) == (2, 0)


def test_main_creator_budget_runs_out_before_its_provider_timeout() -> None:
    """A runaway generation must end as a retryable truncation, never as an
    outcome-unknown timeout that fences the paid call."""
    from app.agents.main_creator import MainCreatorAgent

    runaway_s = _FIRST_TOKEN_S + MainCreatorAgent.max_output_tokens * _S_PER_TOKEN
    assert runaway_s < MainCreatorAgent.spec.timeout_s


def test_runaway_main_creator_call_ends_as_one_truncation_not_an_unknown_outcome() -> None:
    """Behavioral twin of the check above: a call that would think and write
    past its whole output budget stops at MAX_TOKENS inside the provider
    deadline, never as an outcome-unknown fence. At thinking "high" it gets
    exactly one retry at "low" (KRI-542); when that truncates too the run is
    terminal -- never a third paid call."""
    from app.agents.main_creator import MainCreatorAgent

    fixture = json.loads(_KRI178_FIXTURE.read_text())
    # Thinks the whole budget away at every level: nothing is left for the answer.
    client = _SharedBudgetGemini(
        answer=fixture["raw_text"],
        thinking_tokens=MainCreatorAgent.max_output_tokens,
        answer_tokens=2_380,
    )

    with pytest.raises(TerminalError, match="output truncated") as failure:
        MainCreatorAgent(client).run(fixture["input"])

    assert not isinstance(failure.value, (ProviderOutcomeUnknownError, TerminalSchemaError))
    assert [c["thinking_level"] for c in client.calls] == ["high", "low"]
    for call in client.calls:
        # The agent's declared budget and deadline are what reach the provider.
        assert call["max_output_tokens"] == MainCreatorAgent.max_output_tokens
        assert call["timeout_s"] == MainCreatorAgent.spec.timeout_s


# ── KRI-542: a MAX_TOKENS call at thinking "high" is retried once at "low" ───
# Prod 2026-10-08, thread 74dfc456 (M3 Berlin, the turn after a clip-picker
# answer): the Main Creator at "high" (PR 1485) thought past its 8,192 tokens,
# kept 315 for the plan, and the turn dead-ended with "I couldn't turn that
# into a plan this time". Turn 1's two successful calls on the same prompt took
# 48 s and 52 s for ~1.7k answer tokens, i.e. ~6k thinking each: every "high"
# call on a big manifest already sits at 75-95% of the shared budget.


def test_main_creator_truncated_at_high_thinking_is_retried_once_at_low(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.agents import _runtime as runtime_mod
    from app.agents.main_creator import MainCreatorAgent

    runs: list[dict[str, Any]] = []
    warnings: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        runtime_mod.log,
        "info",
        lambda event, **kw: runs.append(dict(kw)) if event == "agent_run" else None,
    )
    monkeypatch.setattr(
        runtime_mod.log, "warning", lambda event, **kw: warnings.append((event, dict(kw)))
    )
    fixture = json.loads(_KRI178_FIXTURE.read_text())
    # The prod shape: thinking leaves ~300 tokens of the budget for the plan.
    high_thinking = MainCreatorAgent.max_output_tokens - 300
    client = _SharedBudgetGemini(
        answer=fixture["raw_text"],
        thinking_tokens=high_thinking,
        answer_tokens=2_380,
        thinking_tokens_by_level={"low": 1_500},
    )

    output = MainCreatorAgent(client).run(fixture["input"])

    assert len(output.action.strategy.reaction_beats) == 13
    assert [c["thinking_level"] for c in client.calls] == ["high", "low"]
    [run] = runs
    assert (run["outcome"], run["attempts"], run["thinking_degraded"]) == ("ok", 2, True)
    assert run["tokens_thoughts"] == high_thinking + 1_500
    [(event, degraded)] = warnings
    assert event == "agent_thinking_degraded"
    assert (degraded["from_level"], degraded["to_level"], degraded["attempt"]) == ("high", "low", 1)
    # Sensitive agent: the warning carries no model text.
    assert degraded["error"] == "sensitive_agent_error"


def test_main_creator_truncated_at_low_thinking_is_terminal_after_one_call() -> None:
    from dataclasses import replace

    from app.agents.main_creator import MainCreatorAgent

    class _LowThinkingMainCreator(MainCreatorAgent):
        spec = replace(MainCreatorAgent.spec, thinking_level="low")

    fixture = json.loads(_KRI178_FIXTURE.read_text())
    client = _SharedBudgetGemini(
        answer=fixture["raw_text"],
        thinking_tokens=MainCreatorAgent.max_output_tokens,
        answer_tokens=2_380,
    )

    with pytest.raises(TerminalError, match="output truncated"):
        _LowThinkingMainCreator(client).run(fixture["input"])

    assert [c["thinking_level"] for c in client.calls] == ["low"]


def test_successful_high_thinking_call_is_not_degraded(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.agents import _runtime as runtime_mod
    from app.agents.main_creator import MainCreatorAgent

    runs: list[dict[str, Any]] = []
    monkeypatch.setattr(
        runtime_mod.log,
        "info",
        lambda event, **kw: runs.append(dict(kw)) if event == "agent_run" else None,
    )
    fixture = json.loads(_KRI178_FIXTURE.read_text())
    client = _SharedBudgetGemini(
        answer=fixture["raw_text"], thinking_tokens=5_800, answer_tokens=2_380
    )

    MainCreatorAgent(client).run(fixture["input"])

    assert [c["thinking_level"] for c in client.calls] == ["high"]
    [run] = runs
    assert run["attempts"] == 1
    assert "thinking_degraded" not in run


@pytest.mark.parametrize(
    ("model", "level", "expected"),
    [
        ("gemini-3.1-pro-preview", "high", "low"),
        ("gemini-3.1-pro-preview", "medium", "low"),
        ("gemini-3.6-flash", " HIGH ", "low"),
        ("gemini-3.1-pro-preview", "low", None),
        ("gemini-3.1-pro-preview", "minimal", None),
        ("gemini-3.1-pro-preview", None, None),
        # Gemini 2.5 budgets thinking separately (`thinking_budget`): unchanged.
        ("gemini-2.5-flash", "high", None),
    ],
)
def test_degraded_thinking_level_table(model: str, level: str | None, expected: str | None) -> None:
    from app.agents._runtime import _degraded_thinking_level

    assert _degraded_thinking_level(model, level) == expected


def test_per_agent_timeout_is_enforced(capturing_client, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from app.agents import _model_client
    from app.agents._runtime import ProviderOutcomeUnknownError

    def slow_generate(**kwargs):  # noqa: ARG001
        time.sleep(0.2)
        return SimpleNamespace(text='{"ranked": []}', usage_metadata=None)

    monkeypatch.setattr(capturing_client, "generate_content", slow_generate)
    # A running SDK call may still reach and bill the provider after our local
    # deadline. It must remain outcome-unknown so the runtime will not overlap
    # it with a retry.
    # Join the fake provider and its late-result callback before pytest changes
    # output capture for the next test. The real client remains nonblocking.
    with ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(_model_client, "_GEMINI_INVOKE_POOL", pool)
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
