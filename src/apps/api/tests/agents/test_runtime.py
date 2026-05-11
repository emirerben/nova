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
    ModelInvocation,
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
    max_tokens_response,
    safety_response,
)

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


def test_refusal_once_then_success(sample_agent: SampleAgent, mock_client: MockModelClient) -> None:
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


def test_max_tokens_finish_reason_terminal_schema(
    sample_agent: SampleAgent, mock_client: MockModelClient
) -> None:
    """Gemini's MAX_TOKENS finish_reason surfaces as a distinct terminal
    SchemaError with a self-documenting message — not the generic 'empty
    response' / 'Invalid JSON' fall-through. Discovered when
    `gemini-2.5-pro`'s thinking step consumed the full 400-token budget on
    CreativeDirectionAgent in prod, leaving zero tokens for the response."""
    truncated = ModelInvocation(
        raw_text="",
        raw_response=max_tokens_response(),
        tokens_in=5,
        tokens_out=0,
    )
    mock_client.queue("gemini-2.5-flash", truncated)
    with pytest.raises(TerminalError) as exc_info:
        sample_agent.run(SampleInput(topic="x"))
    msg = str(exc_info.value)
    assert "schema" in msg.lower()
    assert "MAX_TOKENS" in msg
    assert "Increase max_output_tokens" in msg
    # Terminal on first call — no schema-clarification retry burns another
    # model call, because raising the budget is the only fix.
    assert len(mock_client.invocations) == 1


def test_max_tokens_outcome_logged_as_terminal_schema(
    sample_agent: SampleAgent,
    mock_client: MockModelClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The agent_run log records `outcome=terminal_schema` with the
    MAX_TOKENS truncation message, so it's distinguishable from generic
    schema failures in observability dashboards."""
    captured: list[tuple[str, dict]] = []

    from app.agents import _runtime as runtime_mod

    def fake_info(event: str, **kwargs: object) -> None:
        captured.append((event, dict(kwargs)))

    monkeypatch.setattr(runtime_mod.log, "info", fake_info)
    monkeypatch.setattr(runtime_mod.log, "warning", lambda *a, **kw: None)

    truncated = ModelInvocation(
        raw_text="",
        raw_response=max_tokens_response(),
        tokens_in=5,
        tokens_out=0,
    )
    mock_client.queue("gemini-2.5-flash", truncated)
    with pytest.raises(TerminalError):
        sample_agent.run(SampleInput(topic="x"))

    runs = [c for c in captured if c[0] == "agent_run"]
    assert len(runs) == 1
    payload = runs[0][1]
    assert payload["outcome"] == "terminal_schema"
    assert "MAX_TOKENS" in payload["error"]


def test_refusal_twice_terminal(sample_agent: SampleAgent, mock_client: MockModelClient) -> None:
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


# ── enable_clarification_retries=False (skip-retry agents) ───────────────────


def _no_retry_agent(mock_client: MockModelClient) -> SampleAgent:
    """SampleAgent variant with clarification retries disabled.

    Mirrors ClipMetadataAgent's prod config: a SchemaError or RefusalError
    must raise on the first failure, not burn a second 100-second model
    call. Caller is expected to fall through to a backup path (Whisper).
    """
    import dataclasses  # noqa: PLC0415

    class NoRetryAgent(SampleAgent):
        spec = dataclasses.replace(
            SampleAgent.spec,
            enable_clarification_retries=False,
        )

    return NoRetryAgent(mock_client)


def test_schema_error_no_retry_when_disabled(mock_client: MockModelClient) -> None:
    """Slice-4 contract: with clarification retries off, the FIRST schema
    error raises TerminalError without a second model call."""
    agent = _no_retry_agent(mock_client)
    mock_client.queue(
        "gemini-2.5-flash",
        {"answer": "ok", "score": 999},  # schema error (out of range)
        {"answer": "ok", "score": 50},  # would be the retry — must NOT be invoked
    )
    with pytest.raises(TerminalError) as exc_info:
        agent.run(SampleInput(topic="x"))
    assert "schema" in str(exc_info.value).lower()
    # Critical: only ONE model invocation, not two. The second queued response
    # is left untouched, proving the retry was skipped.
    assert len(mock_client.invocations) == 1


def test_refusal_no_retry_when_disabled(mock_client: MockModelClient) -> None:
    """Same contract for refusals: skip the clarification retry."""
    agent = _no_retry_agent(mock_client)
    mock_client.queue(
        "gemini-2.5-flash",
        {"answer": "partial"},  # missing required `score` → refusal
        {"answer": "ok", "score": 50},  # would be the retry — must NOT be invoked
    )
    with pytest.raises(TerminalError) as exc_info:
        agent.run(SampleInput(topic="x"))
    assert "refusal" in str(exc_info.value).lower()
    assert len(mock_client.invocations) == 1


def test_transient_retries_still_work_when_clarification_disabled(
    mock_client: MockModelClient,
) -> None:
    """Slice 4 ONLY skips clarification retries. TransientError retry must
    still work — those are 5xx/429/timeout, where retrying is correct."""
    agent = _no_retry_agent(mock_client)
    mock_client.queue(
        "gemini-2.5-flash",
        TransientError("503 service unavailable"),
        {"answer": "ok", "score": 50},
    )
    out = agent.run(SampleInput(topic="x"))
    assert out.score == 50
    # Two invocations: one transient failure, one success.
    assert len(mock_client.invocations) == 2


def test_default_spec_keeps_clarification_retries_on(
    sample_agent: SampleAgent, mock_client: MockModelClient
) -> None:
    """Backward-compat sanity: agents that don't set the flag still retry
    once on schema error (existing behavior, covered by the test above —
    pin the default value of the new field here too)."""
    from app.agents._runtime import AgentSpec

    spec = AgentSpec(name="x", prompt_id="x", prompt_version="0", model="m")
    assert spec.enable_clarification_retries is True
    # And SampleAgent (used everywhere in this file) defaults to True too.
    assert sample_agent.spec.enable_clarification_retries is True


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


# ── Effective-model reporting (Fix 3) ─────────────────────────────────────────


def test_model_used_overrides_spec_model_in_log(
    sample_agent: SampleAgent,
    mock_client: MockModelClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the client populates `ModelInvocation.model_used` (because the
    Gemini client rewrote gemini-2.5-flash → settings.gemini_model), the
    agent_run log records the actually-invoked model, not the spec's
    declared model. Closes the observability gap where logs said
    `gemini-2.5-flash` while the HTTP call hit `gemini-2.5-pro`."""
    captured: list[tuple[str, dict]] = []

    from app.agents import _runtime as runtime_mod

    def fake_info(event: str, **kwargs: object) -> None:
        captured.append((event, dict(kwargs)))

    monkeypatch.setattr(runtime_mod.log, "info", fake_info)
    monkeypatch.setattr(runtime_mod.log, "warning", lambda *a, **kw: None)

    # Simulate the GeminiClient rewrite: spec asks for gemini-2.5-flash, the
    # client invoked gemini-2.5-pro and reports that on `model_used`.
    inv = ModelInvocation(
        raw_text='{"answer": "ok", "score": 50}',
        tokens_in=10,
        tokens_out=20,
        model_used="gemini-2.5-pro",
    )
    mock_client.queue("gemini-2.5-flash", inv)
    sample_agent.run(SampleInput(topic="x"))

    runs = [c for c in captured if c[0] == "agent_run"]
    assert len(runs) == 1
    assert runs[0][1]["model"] == "gemini-2.5-pro"


def test_model_used_none_falls_back_to_spec_model(
    sample_agent: SampleAgent,
    mock_client: MockModelClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backward-compat: when the client does NOT set model_used (the common
    case — Whisper, or Gemini when the rewrite was a no-op), the log uses
    the spec's declared model."""
    captured: list[tuple[str, dict]] = []

    from app.agents import _runtime as runtime_mod

    def fake_info(event: str, **kwargs: object) -> None:
        captured.append((event, dict(kwargs)))

    monkeypatch.setattr(runtime_mod.log, "info", fake_info)
    monkeypatch.setattr(runtime_mod.log, "warning", lambda *a, **kw: None)

    # Default ModelInvocation from `mock_client.queue({...})` does not set
    # model_used, so the spec model should be logged.
    mock_client.queue("gemini-2.5-flash", {"answer": "ok", "score": 1})
    sample_agent.run(SampleInput(topic="x"))

    runs = [c for c in captured if c[0] == "agent_run"]
    assert runs[0][1]["model"] == "gemini-2.5-flash"


def test_model_invocation_dataclass_carries_model_used() -> None:
    """The new field on ModelInvocation defaults to None and accepts a
    string — pin both behaviors so future refactors don't silently drop it."""
    default = ModelInvocation(raw_text="hi")
    assert default.model_used is None
    overridden = ModelInvocation(raw_text="hi", model_used="gemini-2.5-pro")
    assert overridden.model_used == "gemini-2.5-pro"


# ── CreativeDirectionAgent budget (Fix 1) ─────────────────────────────────────


def test_creative_direction_max_output_tokens_is_2048() -> None:
    """Pin the budget to 2048: Gemini-2.5-pro's thinking step consumed 397/400
    in prod, leaving zero for the visible response and breaking every
    template analysis. 2048 leaves comfortable headroom without being wasteful."""
    from app.agents.creative_direction import CreativeDirectionAgent

    assert CreativeDirectionAgent.max_output_tokens == 2048
