from contextlib import nullcontext
from uuid import uuid4

import pytest

from app.agents._runtime import (
    AiBudgetExceededError,
    ProviderOutcomeUnknownError,
    ProviderQuotaExceededError,
    TerminalError,
)
from app.tasks import creator_preparation as task


@pytest.mark.parametrize(
    ("stage", "error", "expected_code", "retryable", "private_detail"),
    [
        (
            "analysis",
            ProviderQuotaExceededError(provider="gemini", reason="monthly_spend_limit"),
            "provider_quota_exceeded",
            True,
            "monthly_spend_limit",
        ),
        (
            "resume",
            ProviderQuotaExceededError(provider="gemini", reason="monthly_spend_limit"),
            "provider_quota_exceeded",
            True,
            "monthly_spend_limit",
        ),
        (
            "analysis",
            AiBudgetExceededError(
                scope="creator", reset_at="tomorrow", cached_behavior_available=False
            ),
            "ai_budget_exhausted",
            True,
            "tomorrow",
        ),
        (
            "resume",
            AiBudgetExceededError(
                scope="creator", reset_at="tomorrow", cached_behavior_available=False
            ),
            "ai_budget_exhausted",
            True,
            "tomorrow",
        ),
        (
            "analysis",
            ProviderOutcomeUnknownError("provider still running"),
            "provider_outcome_unknown",
            False,
            "provider still running",
        ),
        (
            "resume",
            ProviderOutcomeUnknownError("provider still running"),
            "provider_outcome_unknown",
            False,
            "provider still running",
        ),
        (
            "analysis",
            PermissionError("private storage payload"),
            "media_unavailable",
            False,
            "private storage payload",
        ),
        (
            "resume",
            PermissionError("private storage payload"),
            "media_unavailable",
            False,
            "private storage payload",
        ),
        (
            "analysis",
            ValueError("replaced generation"),
            "media_unavailable",
            False,
            "replaced generation",
        ),
        (
            "resume",
            ValueError("replaced generation"),
            "media_unavailable",
            False,
            "replaced generation",
        ),
        (
            "analysis",
            RuntimeError("analysis SECRET_PAYLOAD"),
            "analysis_unavailable",
            True,
            "SECRET_PAYLOAD",
        ),
        (
            "resume",
            RuntimeError("planner terminal_schema PRIVATE_RESPONSE"),
            "planning_unavailable",
            True,
            "PRIVATE_RESPONSE",
        ),
        (
            "resume",
            TerminalError("planner terminal_schema PRIVATE_RESPONSE"),
            "planning_unavailable",
            True,
            "PRIVATE_RESPONSE",
        ),
    ],
)
def test_prepare_task_maps_failures_to_safe_public_categories(
    monkeypatch, stage, error, expected_code, retryable, private_detail
) -> None:
    attempt_id = uuid4()
    token = "lease-token"
    monkeypatch.setattr(
        task,
        "_claim",
        lambda _identifier: (token, {}, [], "creator-1", "session-1"),
    )
    monkeypatch.setattr(task, "pipeline_trace_for", lambda _job_id: nullcontext())
    if stage == "analysis":
        monkeypatch.setattr(
            task, "_analyze_sources", lambda *_args, **_kwargs: (_ for _ in ()).throw(error)
        )
    else:
        monkeypatch.setattr(task, "_analyze_sources", lambda *_args, **_kwargs: None)

        async def resume(*_args, **_kwargs):
            raise error

        monkeypatch.setattr(task, "_resume", resume)
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
    assert private_detail not in repr(failed)
