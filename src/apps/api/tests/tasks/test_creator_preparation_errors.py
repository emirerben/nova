from contextlib import nullcontext
from uuid import uuid4

import pytest

from app.agents._runtime import (
    AiBudgetExceededError,
    ProviderOutcomeUnknownError,
    ProviderQuotaExceededError,
)
from app.tasks import creator_preparation as task


@pytest.mark.parametrize(
    ("error", "expected_code", "retryable"),
    [
        (
            ProviderQuotaExceededError(provider="gemini", reason="monthly_spend_limit"),
            "provider_quota_exceeded",
            True,
        ),
        (
            AiBudgetExceededError(
                scope="creator", reset_at="tomorrow", cached_behavior_available=False
            ),
            "ai_budget_exhausted",
            True,
        ),
        (ProviderOutcomeUnknownError("provider still running"), "provider_outcome_unknown", False),
        (PermissionError("private storage payload"), "media_unavailable", False),
        (ValueError("replaced generation"), "media_unavailable", False),
        (RuntimeError("provider response SECRET_PAYLOAD"), "analysis_unavailable", True),
    ],
)
def test_prepare_task_maps_failures_to_safe_public_categories(
    monkeypatch, error, expected_code, retryable
) -> None:
    attempt_id = uuid4()
    token = "lease-token"
    monkeypatch.setattr(
        task,
        "_claim",
        lambda _identifier: (token, {}, [], "creator-1", "session-1"),
    )
    monkeypatch.setattr(task, "pipeline_trace_for", lambda _job_id: nullcontext())
    monkeypatch.setattr(
        task, "_analyze_sources", lambda *_args, **_kwargs: (_ for _ in ()).throw(error)
    )
    failed = []
    monkeypatch.setattr(
        task,
        "_fail",
        lambda identifier, received_token, code, *, retryable: failed.append(
            (identifier, received_token, code, retryable)
        ),
    )

    task.prepare_creator_clips.run(str(attempt_id))

    assert failed == [(attempt_id, token, expected_code, retryable)]
    assert "SECRET_PAYLOAD" not in repr(failed)
    assert "private storage payload" not in repr(failed)
