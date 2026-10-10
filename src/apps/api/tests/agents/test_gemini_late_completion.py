"""A provider reply after the deadline is diagnostic evidence, never a new edit."""

from concurrent.futures import Future, TimeoutError
from types import SimpleNamespace

import pytest

from app.agents import _model_client as module
from app.agents._runtime import ProviderOutcomeUnknownError


class PendingProvider(Future):
    def result(self, timeout=None):
        if not self.done():
            raise TimeoutError()
        return super().result(timeout=timeout)


@pytest.mark.parametrize("fails", [False, True])
def test_late_provider_outcome_is_correlated_without_exposing_content(monkeypatch, fails):
    future = PendingProvider()
    future.set_running_or_notify_cancel()
    calls = []
    events = []

    def submit(*args, **kwargs):
        calls.append((args, kwargs))
        return future

    def record(event, **fields):
        events.append({"event": event, **fields})

    monkeypatch.setattr(module, "_GEMINI_INVOKE_POOL", SimpleNamespace(submit=submit))
    monkeypatch.setattr(module, "log", SimpleNamespace(info=record, warning=record))
    monkeypatch.setattr(
        module.GeminiClient,
        "_get",
        lambda self: SimpleNamespace(models=SimpleNamespace(generate_content=lambda: None)),
    )
    with pytest.raises(ProviderOutcomeUnknownError):
        module.GeminiClient().invoke(
            model="gemini-3.1-pro-preview", prompt="private creator request", timeout_s=40
        )

    if fails:
        future.set_exception(RuntimeError("private provider failure body"))
    else:
        future.set_result(
            SimpleNamespace(
                text="private generated edit",
                response_id="provider-response-123",
                usage_metadata=SimpleNamespace(
                    prompt_token_count=30000, candidates_token_count=800, thoughts_token_count=5000
                ),
            )
        )

    timeout = next(e for e in events if e["event"] == "gemini_provider_deadline_exceeded")
    late = next(e for e in events if e["event"] == "gemini_provider_late_completion")
    assert timeout["provider_call_id"] == late["provider_call_id"]
    assert late["outcome"] == ("error" if fails else "response")
    assert len(calls) == 1
    assert "private" not in str(events)
    if fails:
        assert late["error_class"] == "RuntimeError"
    else:
        assert late["provider_request_id"] == "provider-response-123"
        assert late["tokens_thoughts"] == 5000
        assert late["tokens_in"] == 30000
