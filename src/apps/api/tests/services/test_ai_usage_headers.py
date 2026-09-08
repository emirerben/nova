from __future__ import annotations

from starlette.requests import Request

from app.config import settings
from app.services.ai_usage_headers import paid_call_headers


def _request(**headers: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [(key.lower().encode(), value.encode()) for key, value in headers.items()],
        }
    )


def test_production_ignores_creator_supplied_test_and_customer_classification(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ai_usage_environment", "production")
    parsed = paid_call_headers(
        _request(
            **{
                "x-nova-usage-purpose": "customer",
                "x-nova-test-run-id": "fake",
                "x-nova-estimated-max-cost-usd": "2",
                "x-nova-reservation-approved": "true",
            }
        ),
        usage_purpose="style_vision",
    )

    assert parsed.usage_purpose == "style_vision"
    assert parsed.test_run_id is None
    assert parsed.estimated_max_cost_usd is None
    assert parsed.reservation_approved is False


def test_production_canary_headers_remain_subject_to_ledger_internal_user_check(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "ai_usage_environment", "production")
    parsed = paid_call_headers(
        _request(
            **{
                "x-nova-usage-purpose": "release_canary",
                "x-nova-release-canary-id": "release-42",
            }
        )
    )

    assert parsed.usage_purpose == "release_canary"
    assert parsed.release_canary_id == "release-42"


def test_development_accepts_explicit_paid_test_envelope(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ai_usage_environment", "development")
    parsed = paid_call_headers(
        _request(
            **{
                "x-nova-usage-purpose": "manual_qa",
                "x-nova-test-run-id": "qa-1",
                "x-nova-estimated-max-cost-usd": "0.5",
                "x-nova-reservation-approved": "true",
            }
        )
    )

    assert parsed.usage_purpose == "manual_qa"
    assert parsed.test_run_id == "qa-1"
    assert parsed.estimated_max_cost_usd == 0.5
    assert parsed.reservation_approved is True
