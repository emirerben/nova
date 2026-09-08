from __future__ import annotations

import json

import pytest
from starlette.requests import Request

from app.agents._runtime import (
    AiBudgetExceededError,
    AiCostControlPolicyError,
    CostControlUnavailableError,
    ProviderOutcomeUnknownError,
)
from app.main import (
    ai_budget_exhausted_handler,
    ai_cost_control_unavailable_handler,
    ai_cost_policy_handler,
    ai_provider_outcome_unknown_handler,
)


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/plan/items/example/director",
            "raw_path": b"/plan/items/example/director",
            "query_string": b"",
            "headers": [
                (b"origin", b"https://www.usekria.com"),
                (b"x-request-id", b"budget-request-1"),
            ],
            "client": ("127.0.0.1", 1),
            "server": ("test", 443),
        }
    )


@pytest.mark.asyncio
async def test_budget_error_has_one_cache_aware_429_contract() -> None:
    response = await ai_budget_exhausted_handler(
        _request(),
        AiBudgetExceededError(
            scope="edit_director:user_day",
            reset_at="2026-09-09T00:00:00+00:00",
            cached_behavior_available=True,
        ),
    )
    body = json.loads(bytes(response.body))

    assert response.status_code == 429
    assert body == {
        "detail": "AI budget exhausted for this scope.",
        "code": "ai_budget_exhausted",
        "scope": "edit_director:user_day",
        "reset_at": "2026-09-09T00:00:00+00:00",
        "cached_behavior_available": True,
        "reason": "ai_budget_exhausted",
        "retryable": False,
        "request_id": "budget-request-1",
        "correlation_id": "budget-request-1",
    }
    assert response.headers["access-control-allow-origin"] == "https://www.usekria.com"


@pytest.mark.asyncio
async def test_attribution_policy_error_is_not_reported_as_budget_exhaustion() -> None:
    response = await ai_cost_policy_handler(
        _request(),
        AiCostControlPolicyError(
            scope="development:attribution",
            reason="paid_test_attribution_required",
            status_code=422,
        ),
    )
    body = json.loads(bytes(response.body))

    assert response.status_code == 422
    assert body["code"] == "ai_cost_control_policy_rejected"
    assert body["reason"] == "paid_test_attribution_required"
    assert "reset_at" not in body


@pytest.mark.asyncio
async def test_control_plane_failure_is_retryable_503() -> None:
    response = await ai_cost_control_unavailable_handler(
        _request(), CostControlUnavailableError("ledger unavailable")
    )
    body = json.loads(bytes(response.body))

    assert response.status_code == 503
    assert body["code"] == "ai_cost_control_unavailable"
    assert body["retryable"] is True


@pytest.mark.asyncio
async def test_provider_unknown_is_nonretryable_503() -> None:
    response = await ai_provider_outcome_unknown_handler(
        _request(), ProviderOutcomeUnknownError("provider may have accepted the call")
    )
    body = json.loads(bytes(response.body))

    assert response.status_code == 503
    assert body["code"] == "ai_provider_outcome_unknown"
    assert body["retryable"] is False
    assert "Do not retry" in body["detail"]
