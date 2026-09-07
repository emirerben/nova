from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.services.kria_trace import reconciliation_actions, trace_alerts, trace_metrics

ADMIN_TOKEN = "test-admin-token"


@pytest.fixture()
def client():
    return TestClient(app, raise_server_exceptions=False)


def _trace() -> dict:
    now = datetime.now(UTC)
    return {
        "schema_version": 1,
        "redacted": True,
        "thread": {"thread_id": "thread-1", "runtime_version": 2},
        "turns": [
            {
                "turn_id": "turn-1",
                "status": "planning",
                "lease_expires_at": (now - timedelta(seconds=1)).isoformat(),
            }
        ],
        "drafts": [],
        "approvals": [{"approval_id": "approval-1", "status": "approved"}],
        "executions": [{"execution_id": "execution-1", "status": "outcome_unknown"}],
        "jobs": [{"job_id": "job-1", "status": "processing"}],
        "events": [{"event_type": "approval_requested"}],
    }


def test_reconciliation_projection_is_specific_and_read_only() -> None:
    actions = reconciliation_actions(_trace())
    assert actions == [
        {"action": "reclaim_expired_turn", "turn_id": "turn-1"},
        {"action": "publish_approval", "approval_id": "approval-1"},
        {"action": "reconcile_dispatch", "execution_id": "execution-1"},
    ]
    assert trace_metrics(_trace()) == {
        "turn_statuses": {"planning": 1},
        "execution_statuses": {"outcome_unknown": 1},
        "approval_statuses": {"approved": 1},
        "job_statuses": {"processing": 1},
        "recovery_action_count": 3,
        "alert_count": 1,
        "useful_response_latency_ms": {"count": 0, "p50": None, "p95": None, "max": None},
        "approval_to_observation_latency_ms": {
            "count": 0,
            "p50": None,
            "p95": None,
            "max": None,
        },
    }
    assert trace_alerts(_trace()) == [
        {
            "code": "expired_turn_lease",
            "severity": "warning",
            "turn_id": "turn-1",
            "action": "reclaim_expired_turn",
        }
    ]


def test_trace_requires_admin_auth(client: TestClient) -> None:
    with patch("app.routes.admin.settings") as settings:
        settings.admin_api_key = ADMIN_TOKEN
        response = client.get("/admin/kria/trace?thread_id=00000000-0000-0000-0000-000000000001")
    assert response.status_code in {401, 422}


def test_trace_lookup_returns_redacted_chain(client: TestClient) -> None:
    async def _db():
        yield AsyncMock()

    app.dependency_overrides[get_db] = _db
    try:
        with (
            patch("app.routes.admin.settings") as settings,
            patch(
                "app.routes.admin_kria.resolve_kria_trace",
                new=AsyncMock(return_value=_trace()),
            ),
        ):
            settings.admin_api_key = ADMIN_TOKEN
            response = client.get(
                "/admin/kria/trace?turn_id=00000000-0000-0000-0000-000000000001",
                headers={"X-Admin-Token": ADMIN_TOKEN},
            )
    finally:
        app.dependency_overrides.pop(get_db, None)
    assert response.status_code == 200
    body = response.json()
    assert body["trace"]["redacted"] is True
    assert body["recovery_actions"][0]["action"] == "reclaim_expired_turn"
    assert body["alerts"][0]["code"] == "expired_turn_lease"
    assert "content" not in str(body["trace"]["events"])


def test_trace_identity_is_exact(client: TestClient) -> None:
    with patch("app.routes.admin.settings") as settings:
        settings.admin_api_key = ADMIN_TOKEN
        response = client.get("/admin/kria/trace", headers={"X-Admin-Token": ADMIN_TOKEN})
    assert response.status_code == 422
    assert response.json()["detail"] == "provide_exactly_one_trace_identity"


def test_dry_run_uses_same_non_mutating_projection(client: TestClient) -> None:
    async def _db():
        yield AsyncMock()

    app.dependency_overrides[get_db] = _db
    try:
        with (
            patch("app.routes.admin.settings") as settings,
            patch(
                "app.routes.admin_kria.resolve_kria_trace",
                new=AsyncMock(return_value=_trace()),
            ),
        ):
            settings.admin_api_key = ADMIN_TOKEN
            response = client.post(
                "/admin/kria/reconcile/dry-run?thread_id=00000000-0000-0000-0000-000000000001",
                headers={"X-Admin-Token": ADMIN_TOKEN},
            )
    finally:
        app.dependency_overrides.pop(get_db, None)
    assert response.status_code == 200
    assert response.json()["metrics"]["recovery_action_count"] == 3


def test_trace_metrics_measure_latency_and_alert_stalled_dispatch() -> None:
    now = datetime.now(UTC)
    trace = _trace()
    trace["turns"] = [
        {
            "turn_id": "turn-1",
            "status": "completed",
            "created_at": (now - timedelta(seconds=8)).isoformat(),
            "completed_at": now.isoformat(),
            "lease_expires_at": None,
        }
    ]
    trace["executions"] = [
        {
            "execution_id": "execution-1",
            "status": "accepted",
            "risk": "approval_required",
            "started_at": (now - timedelta(minutes=3)).isoformat(),
            "accepted_at": (now - timedelta(seconds=30)).isoformat(),
            "observed_at": now.isoformat(),
            "job_id": "job-1",
            "generation_id": "generation-1",
        }
    ]
    metrics = trace_metrics(trace)

    assert metrics["useful_response_latency_ms"]["p95"] == 8_000
    assert metrics["approval_to_observation_latency_ms"]["p95"] == 30_000
    assert trace_alerts(trace, now=now)[0]["code"] == "accepted_dispatch_stalled"


def test_trace_alerts_cover_expiry_unknown_observer_lag_and_duplicate_generation() -> None:
    now = datetime.now(UTC)
    stale = (now - timedelta(minutes=3)).isoformat()
    trace = _trace()
    trace["turns"] = []
    trace["approvals"] = [
        {"approval_id": "approval-expired", "status": "pending", "expires_at": stale}
    ]
    trace["jobs"] = [
        {
            "job_id": "job-terminal",
            "status": "variants_ready_partial",
            "finished_at": stale,
            "created_at": stale,
        }
    ]
    trace["executions"] = [
        {
            "execution_id": "unknown",
            "status": "outcome_unknown",
            "started_at": stale,
            "risk": "approval_required",
            "job_id": "job-duplicate",
            "generation_id": "generation-1",
        },
        {
            "execution_id": "observer",
            "status": "dispatched",
            "started_at": stale,
            "dispatched_at": stale,
            "risk": "approval_required",
            "job_id": "job-terminal",
            "generation_id": "generation-2",
        },
        {
            "execution_id": "duplicate-a",
            "status": "completed",
            "risk": "approval_required",
            "job_id": "job-duplicate",
            "generation_id": "generation-1",
        },
    ]

    alerts = trace_alerts(trace, now=now)
    assert {alert["code"] for alert in alerts} == {
        "expired_pending_approval",
        "dispatch_outcome_unknown_stale",
        "terminal_job_observer_lag",
        "duplicate_approved_generation",
    }
