"""Spike tests for app.agents._langfuse — observability fail-open contract.

The contract: tracing must NEVER break Agent.run() and must NEVER be required.
- No env vars + no SDK → no-op, no errors
- Env vars set but SDK missing → warning logged, no-op
- Env vars set + SDK present → trace posted, errors swallowed
"""

from __future__ import annotations

import importlib
import sys
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch):
    """Each test gets a fresh _langfuse module so the lazy singleton resets."""
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    # Force re-init by removing the cached module
    sys.modules.pop("app.agents._langfuse", None)
    yield
    sys.modules.pop("app.agents._langfuse", None)


def test_trace_agent_run_noop_without_env_vars():
    """No env vars → no client, no error, no SDK import attempted."""
    from app.agents._langfuse import _get_client, trace_agent_run

    assert _get_client() is None
    # Must not raise
    trace_agent_run(
        agent_name="nova.test.agent",
        prompt_version="v1",
        model="gemini-2.5-flash",
        outcome="ok",
        input_dict={"x": 1},
        output_dict={"y": 2},
        tokens_in=100,
        tokens_out=50,
    )


def test_trace_agent_run_noop_when_sdk_missing(monkeypatch):
    """Env vars set but `langfuse` package not installed → warning + no-op."""
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")

    # Simulate ImportError on `from langfuse import Langfuse`
    import builtins

    real_import = builtins.__import__

    def block_langfuse(name, *args, **kwargs):
        if name == "langfuse":
            raise ImportError("No module named 'langfuse'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", block_langfuse)

    from app.agents._langfuse import _get_client, trace_agent_run

    assert _get_client() is None
    # Must not raise
    trace_agent_run(
        agent_name="nova.test.agent",
        prompt_version="v1",
        model="gemini-2.5-flash",
        outcome="ok",
    )


def test_trace_agent_run_calls_sdk_when_available(monkeypatch):
    """Env vars + SDK present → client built, trace + generation called."""
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")

    # Inject a fake `langfuse` module before the lazy import fires
    fake_lf_module = MagicMock()
    fake_client = MagicMock()
    fake_trace = MagicMock()
    fake_client.trace.return_value = fake_trace
    fake_lf_module.Langfuse.return_value = fake_client
    monkeypatch.setitem(sys.modules, "langfuse", fake_lf_module)

    from app.agents._langfuse import trace_agent_run

    trace_agent_run(
        agent_name="nova.compose.template_recipe",
        prompt_version="2026-05-09",
        model="gemini-2.5-flash",
        outcome="ok",
        input_dict={"file_uri": "files/x"},
        output_dict={"shot_count": 4},
        tokens_in=1200,
        tokens_out=800,
        cost_usd=0.000180,
        latency_ms=4200,
        attempts=1,
        fallback_used=False,
        job_id="job-abc",
    )

    fake_lf_module.Langfuse.assert_called_once()
    fake_client.trace.assert_called_once()
    call_kwargs = fake_client.trace.call_args.kwargs
    assert call_kwargs["name"] == "nova.compose.template_recipe"
    assert call_kwargs["session_id"] == "job-abc"
    assert call_kwargs["input"] == {"file_uri": "files/x"}
    assert call_kwargs["output"] == {"shot_count": 4}
    assert "ok" in call_kwargs["tags"]

    fake_trace.generation.assert_called_once()
    gen_kwargs = fake_trace.generation.call_args.kwargs
    assert gen_kwargs["model"] == "gemini-2.5-flash"
    assert gen_kwargs["usage"]["input"] == 1200
    assert gen_kwargs["usage"]["output"] == 800
    assert gen_kwargs["metadata"]["cost_usd"] == 0.000180
    assert gen_kwargs["level"] == "DEFAULT"


def test_trace_agent_run_marks_errors_as_error_level(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")

    fake_lf_module = MagicMock()
    fake_client = MagicMock()
    fake_trace = MagicMock()
    fake_client.trace.return_value = fake_trace
    fake_lf_module.Langfuse.return_value = fake_client
    monkeypatch.setitem(sys.modules, "langfuse", fake_lf_module)

    from app.agents._langfuse import trace_agent_run

    trace_agent_run(
        agent_name="nova.test.agent",
        prompt_version="v1",
        model="gemini-2.5-flash",
        outcome="terminal_schema",
        error="parse failed: bad json",
    )

    gen_kwargs = fake_trace.generation.call_args.kwargs
    assert gen_kwargs["level"] == "ERROR"
    assert gen_kwargs["status_message"] == "parse failed: bad json"


def test_trace_agent_run_swallows_sdk_exceptions(monkeypatch):
    """If the SDK raises mid-trace, Agent.run() must not see the error."""
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")

    fake_lf_module = MagicMock()
    fake_client = MagicMock()
    fake_client.trace.side_effect = RuntimeError("langfuse network broke")
    fake_lf_module.Langfuse.return_value = fake_client
    monkeypatch.setitem(sys.modules, "langfuse", fake_lf_module)

    from app.agents._langfuse import trace_agent_run

    # Must not raise
    trace_agent_run(
        agent_name="nova.test.agent",
        prompt_version="v1",
        model="gemini-2.5-flash",
        outcome="ok",
    )


def test_runtime_calls_trace_on_successful_agent_run(monkeypatch):
    """End-to-end: a real Agent.run() with SDK mocked → trace posted with
    real agent metadata + input/output dicts."""
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")

    fake_lf_module = MagicMock()
    fake_client = MagicMock()
    fake_trace = MagicMock()
    fake_client.trace.return_value = fake_trace
    fake_lf_module.Langfuse.return_value = fake_client
    monkeypatch.setitem(sys.modules, "langfuse", fake_lf_module)

    # Reload runtime so its `from _langfuse import` cache is fresh
    importlib.import_module("app.agents._langfuse")

    from pydantic import BaseModel

    from app.agents._runtime import (
        Agent,
        AgentSpec,
        ModelClient,
        ModelInvocation,
        RunContext,
    )

    class _Input(BaseModel):
        x: int

    class _Output(BaseModel):
        y: int

    class _StubClient(ModelClient):
        def invoke(self, **kwargs):
            return ModelInvocation(raw_text='{"y": 42}', tokens_in=10, tokens_out=5)

    class _StubAgent(Agent[_Input, _Output]):
        spec = AgentSpec(
            name="nova.test.stub",
            prompt_id="_inline",
            prompt_version="2026-05-11",
            model="gemini-2.5-flash",
            max_attempts=1,
            cost_per_1k_input_usd=0.000075,
            cost_per_1k_output_usd=0.0003,
        )
        Input = _Input
        Output = _Output

        def render_prompt(self, input):  # noqa: A002
            return "prompt"

        def parse(self, raw_text, input):  # noqa: A002
            import json

            return _Output(**json.loads(raw_text))

    out = _StubAgent(_StubClient()).run(_Input(x=1), ctx=RunContext(job_id="job-xyz"))
    assert out.y == 42

    fake_client.trace.assert_called_once()
    call_kwargs = fake_client.trace.call_args.kwargs
    assert call_kwargs["name"] == "nova.test.stub"
    assert call_kwargs["session_id"] == "job-xyz"
    assert call_kwargs["input"] == {"x": 1}
    assert call_kwargs["output"] == {"y": 42}
