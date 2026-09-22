from __future__ import annotations

from dataclasses import replace

import pytest
from google.genai import errors as genai_errors

from app.agents._provider_errors import classify_provider_error
from app.agents._runtime import ProviderQuotaExceededError
from tests.agents.conftest import SampleAgent


@pytest.mark.parametrize(
    ("message", "reason"),
    [
        ("Monthly spend limit reached", "monthly_spend_limit"),
        (
            "You have exceeded your current quota, please check your plan and billing details.",
            "quota_exceeded",
        ),
        ("rate limit exceeded", None),
        ("temporary capacity unavailable", None),
    ],
)
def test_google_429_classification_is_narrow(message: str, reason: str | None) -> None:
    error = classify_provider_error(genai_errors.ClientError(429, {"message": message}))
    if reason is None:
        assert error is None
    else:
        assert isinstance(error, ProviderQuotaExceededError)
        assert error.reason == reason
        assert str(error) == "provider quota exceeded"


def test_runtime_preserves_quota_error_for_sensitive_agent(
    sample_agent: SampleAgent, mock_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    class SensitiveQuotaAgent(SampleAgent):
        spec = replace(SampleAgent.spec, sensitive_io=True)

    captured: list[dict] = []
    from app.agents import _runtime as runtime_mod

    monkeypatch.setattr(runtime_mod.log, "info", lambda event, **kw: captured.append(kw))
    monkeypatch.setattr(runtime_mod.log, "warning", lambda *args, **kwargs: None)
    agent = SensitiveQuotaAgent(mock_client)
    error = ProviderQuotaExceededError(provider="gemini", reason="monthly_spend_limit")
    mock_client.queue("gemini-2.5-flash", error)

    with pytest.raises(ProviderQuotaExceededError) as exc_info:
        agent.run({"topic": "private prompt"})

    assert exc_info.value is error
    runs = [payload for payload in captured if payload.get("outcome") == "terminal_provider_quota"]
    assert len(runs) == 1
    assert runs[0]["error"] == "sensitive_agent_error"
