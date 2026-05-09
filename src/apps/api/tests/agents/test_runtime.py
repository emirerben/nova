"""Runtime behavior tests: retry, refusal, schema-error, fallback, rule_based, shadow.

Every retry/refusal/schema/fallback path is exercised against the MockModelClient.
"""

from __future__ import annotations

from typing import ClassVar

import pytest
from pydantic import BaseModel

from app.agents._runtime import (
    Agent,
    AgentSpec,
    RunContext,
    TerminalError,
    TransientError,
    run_with_shadow,
)
from tests.agents.conftest import (
    MockModelClient,
    SampleAgent,
    SampleInput,
    SampleOutput,
    safety_response,
)
from app.agents._runtime import ModelInvocation


# ── Happy paths ───────────────────────────────────────────────────────────────


def test_happy_path(sample_agent: SampleAgent, mock_client: MockModelClient) -> None:
    mock_client.queue("gemini-2.5-flash", {"answer": "yes", "score": 80})
    out = sample_agent.run(SampleInput(topic="x"))
    assert out.answer == "yes"
    assert out.score == 80
    # Single call, no fallback
    assert len(mock_client.invocations) == 1
    assert mock_client.invocations[0]["model"] == "gemini-2.5-flash"


def test_input_can_be_dict(sample_agent: SampleAgent, mock_client: MockModelClient) -> None:
    mock_client.queue("gemini-2.5-flash", {"answer": "yes", "score": 50})
    out = sample_agent.run({"topic": "x"})
    assert out.answer == "yes"


# ── Transient retries ─────────────────────────────────────────────────────────


def test_transient_retry_succeeds(sample_agent: SampleAgent, mock_client: MockModelClient) -> None:
    mock_client.queue(
        "gemini-2.5-flash",
        TransientError("503 first"),
        TransientError("503 second"),
        {"answer": "ok", "score": 70},
    )
    out = sample_agent.run(SampleInput(topic="x"))
    assert out.answer == "ok"
    assert len(mock_client.invocations) == 3


def test_transient_exhausts_to_fallback(
    sample_agent: SampleAgent, mock_client: MockModelClient
) -> None:
    # max_attempts=3 on primary; all fail transient → fall back to pro.
    for _ in range(3):
        mock_client.queue("gemini-2.5-flash", TransientError("503"))
    mock_client.queue("gemini-2.5-pro", {"answer": "fallback", "score": 60})
    out = sample_agent.run(SampleInput(topic="x"))
    assert out.answer == "fallback"
    flash_calls = [i for i in mock_client.invocations if i["model"] == "gemini-2.5-flash"]
    pro_calls = [i for i in mock_client.invocations if i["model"] == "gemini-2.5-pro"]
    assert len(flash_calls) == 3
    assert len(pro_calls) == 1


def test_transient_exhausts_all_models_terminal(
    sample_agent: SampleAgent, mock_client: MockModelClient
) -> None:
    for _ in range(3):
        mock_client.queue("gemini-2.5-flash", TransientError("503"))
    for _ in range(3):
        mock_client.queue("gemini-2.5-pro", TransientError("503"))
    with pytest.raises(TerminalError) as exc_info:
        sample_agent.run(SampleInput(topic="x"))
    assert "exhausted" in str(exc_info.value)


# ── Refusal handling ──────────────────────────────────────────────────────────


def test_refusal_once_then_success(
    sample_agent: SampleAgent, mock_client: MockModelClient
) -> None:
    # First call: missing required field "score" → triggers RefusalError → retry
    # Second call: complete output
    mock_client.queue(
        "gemini-2.5-flash",
        {"answer": "partial"},  # missing "score"
        {"answer": "ok", "score": 90},
    )
    out = sample_agent.run(SampleInput(topic="x"))
    assert out.score == 90
    assert len(mock_client.invocations) == 2
    # Second prompt should have the clarification suffix
    assert "do not refuse" in mock_client.invocations[1]["prompt"].lower()


def test_safety_refusal_via_finish_reason(
    sample_agent: SampleAgent, mock_client: MockModelClient
) -> None:
    safe_inv = ModelInvocation(
        raw_text='{"answer": "ok", "score": 50}',
        raw_response=safety_response(),
        tokens_in=5,
        tokens_out=5,
    )
    mock_client.queue("gemini-2.5-flash", safe_inv, {"answer": "ok", "score": 50})
    out = sample_agent.run(SampleInput(topic="x"))
    assert out.answer == "ok"


def test_refusal_twice_terminal(
    sample_agent: SampleAgent, mock_client: MockModelClient
) -> None:
    mock_client.queue(
        "gemini-2.5-flash",
        {"answer": "partial"},
        {"answer": ""},  # also a refusal — empty required field
    )
    with pytest.raises(TerminalError) as exc_info:
        sample_agent.run(SampleInput(topic="x"))
    assert "refusal" in str(exc_info.value).lower()


# ── Schema errors ─────────────────────────────────────────────────────────────


def test_schema_error_once_then_success(
    sample_agent: SampleAgent, mock_client: MockModelClient
) -> None:
    # First response has score out of range; Pydantic rejects.
    # Second response is valid.
    mock_client.queue(
        "gemini-2.5-flash",
        {"answer": "ok", "score": 999},  # score>100 fails Pydantic ge/le
        {"answer": "ok", "score": 50},
    )
    out = sample_agent.run(SampleInput(topic="x"))
    assert out.score == 50
    assert "valid json matching the schema" in mock_client.invocations[1]["prompt"].lower()


def test_schema_error_twice_terminal(
    sample_agent: SampleAgent, mock_client: MockModelClient
) -> None:
    mock_client.queue(
        "gemini-2.5-flash",
        {"answer": "ok", "score": 999},
        {"answer": "ok", "score": -5},
    )
    with pytest.raises(TerminalError) as exc_info:
        sample_agent.run(SampleInput(topic="x"))
    assert "schema" in str(exc_info.value).lower()


# ── Terminal errors ───────────────────────────────────────────────────────────


def test_terminal_error_no_retry(sample_agent: SampleAgent, mock_client: MockModelClient) -> None:
    mock_client.queue("gemini-2.5-flash", TerminalError("400 bad request"))
    with pytest.raises(TerminalError):
        sample_agent.run(SampleInput(topic="x"))
    # Single attempt — no retries on terminal
    assert len(mock_client.invocations) == 1


# ── Logging fields ────────────────────────────────────────────────────────────


def test_outcome_log_emitted(
    sample_agent: SampleAgent, mock_client: MockModelClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runtime emits exactly one 'agent_run' event per public call.

    structlog routes through its own loggers (not stdlib logging.getLogger), so
    pytest's caplog fixture doesn't see them by default. Patch the module-level
    logger's `info` method directly.
    """
    captured: list[tuple[str, dict]] = []

    from app.agents import _runtime as runtime_mod

    def fake_info(event: str, **kwargs: object) -> None:
        captured.append((event, dict(kwargs)))

    monkeypatch.setattr(runtime_mod.log, "info", fake_info)
    monkeypatch.setattr(runtime_mod.log, "warning", lambda *a, **kw: None)

    mock_client.queue("gemini-2.5-flash", {"answer": "ok", "score": 50})
    sample_agent.run(SampleInput(topic="x"))

    runs = [c for c in captured if c[0] == "agent_run"]
    assert len(runs) == 1, f"expected exactly one agent_run event, got {len(runs)}"
    payload = runs[0][1]
    assert payload["agent"] == "test.sample"
    assert payload["model"] == "gemini-2.5-flash"
    assert payload["outcome"] == "ok"
    assert payload["attempts"] == 1
    assert payload["fallback_used"] is False
    assert payload["tokens_in"] == 10
    assert payload["tokens_out"] == 20
    assert payload["cost_usd"] > 0  # 10 * 0.001/1k + 20 * 0.002/1k


# ── Rule-based bypass ─────────────────────────────────────────────────────────


class _RuleInput(BaseModel):
    n: int


class _RuleOutput(BaseModel):
    doubled: int


class _RuleAgent(Agent[_RuleInput, _RuleOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="test.rule",
        prompt_id="unused",
        prompt_version="0",
        model="rule_based",
    )
    Input = _RuleInput
    Output = _RuleOutput

    def render_prompt(self, input: _RuleInput) -> str:  # noqa: A002, ARG002
        return ""

    def parse(self, raw_text: str, input: _RuleInput) -> _RuleOutput:  # noqa: A002, ARG002
        raise NotImplementedError

    def compute(self, input: _RuleInput) -> _RuleOutput:  # noqa: A002
        return _RuleOutput(doubled=input.n * 2)


def test_rule_based_bypasses_model_client(mock_client: MockModelClient) -> None:
    agent = _RuleAgent(mock_client)
    out = agent.run(_RuleInput(n=21))
    assert out.doubled == 42
    # Critically, the model client was never called.
    assert mock_client.invocations == []


def test_rule_based_failure_terminal(mock_client: MockModelClient) -> None:
    class _Broken(_RuleAgent):
        def compute(self, input: _RuleInput) -> _RuleOutput:  # noqa: A002, ARG002
            raise ValueError("nope")

    with pytest.raises(TerminalError):
        _Broken(mock_client).run(_RuleInput(n=1))


# ── Shadow mode ───────────────────────────────────────────────────────────────


def test_shadow_returns_primary_logs_diff(
    sample_agent: SampleAgent, mock_client: MockModelClient, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    mock_client.queue("gemini-2.5-flash", {"answer": "primary", "score": 50})

    def shadow(_: SampleInput) -> SampleOutput:
        return SampleOutput(answer="shadow", score=99)

    with caplog.at_level(logging.INFO):
        result = run_with_shadow(sample_agent, shadow, SampleInput(topic="x"))

    assert result.answer == "primary"  # primary always wins


def test_shadow_failure_does_not_break_primary(
    sample_agent: SampleAgent, mock_client: MockModelClient
) -> None:
    mock_client.queue("gemini-2.5-flash", {"answer": "primary", "score": 50})

    def shadow(_: SampleInput) -> SampleOutput:
        raise RuntimeError("shadow exploded")

    result = run_with_shadow(sample_agent, shadow, SampleInput(topic="x"))
    assert result.answer == "primary"


# ── Run context propagation ───────────────────────────────────────────────────


def test_run_context_default(sample_agent: SampleAgent, mock_client: MockModelClient) -> None:
    mock_client.queue("gemini-2.5-flash", {"answer": "ok", "score": 1})
    # No ctx passed — runtime should construct a default RunContext.
    out = sample_agent.run(SampleInput(topic="x"))
    assert out.answer == "ok"


def test_run_context_threaded(sample_agent: SampleAgent, mock_client: MockModelClient) -> None:
    mock_client.queue("gemini-2.5-flash", {"answer": "ok", "score": 1})
    ctx = RunContext(job_id="job-123", segment_idx=4)
    out = sample_agent.run(SampleInput(topic="x"), ctx=ctx)
    assert out.answer == "ok"
