from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.auth import get_current_user
from app.config import settings
from app.database import get_db
from app.kria.api_schemas import (
    ApprovalDecisionBody,
    ApprovalDecisionOut,
    ApprovalSnapshotOut,
    DraftSnapshotOut,
    SubmitTurnBody,
    ThreadDeltaOut,
    TurnAccepted,
    TurnCancelBody,
    TurnCancelled,
)
from app.kria.runtime import RuntimeFailure
from app.limiter import limiter
from app.main import app
from app.routes import creation_threads, kria_runtime


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def runtime_defaults(monkeypatch: pytest.MonkeyPatch):
    limiter.reset()
    app.dependency_overrides.clear()
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    yield
    limiter.reset()
    app.dependency_overrides.clear()


def _body(*, expected_thread_revision: int = 0) -> dict[str, object]:
    return {
        "message": "Help me shape this edit",
        "client_event_id": "client-event-1",
        "expected_thread_revision": expected_thread_revision,
    }


def _install_authenticated_user() -> tuple[SimpleNamespace, AsyncMock]:
    user = SimpleNamespace(id=uuid.uuid4(), email="creator@example.com")
    db = AsyncMock()
    db.rollback = AsyncMock()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    return user, db


def test_create_turn_requires_authentication(client: TestClient) -> None:
    db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: db

    response = client.post(f"/creation-threads/{uuid.uuid4()}/turns", json=_body())

    assert response.status_code == 401
    assert response.json()["problem"]["code"] == "authentication_required"
    assert response.json()["problem"]["phase"] == "accept"


def test_framework_validation_uses_typed_problem(client: TestClient) -> None:
    _user, db = _install_authenticated_user()

    response = client.post(
        f"/creation-threads/{uuid.uuid4()}/turns",
        json={
            "message": "   ",
            "client_event_id": "event-1",
            "expected_thread_revision": 0,
        },
    )

    assert response.status_code == 422
    assert response.json()["problem"]["code"] == "request_invalid"
    assert response.json()["problem"]["recovery"] == "manual"
    db.rollback.assert_not_awaited()


def test_turn_rate_limit_uses_typed_problem(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _user, _db = _install_authenticated_user()
    turn_id = str(uuid.uuid4())
    monkeypatch.setattr(
        kria_runtime,
        "submit_turn",
        AsyncMock(
            return_value=(
                TurnAccepted(
                    turn_id=turn_id,
                    thread_revision=1,
                    status="queued",
                ),
                False,
            )
        ),
    )
    thread_id = uuid.uuid4()

    responses = [
        client.post(
            f"/creation-threads/{thread_id}/turns",
            json={**_body(), "client_event_id": f"event-{index}"},
        )
        for index in range(13)
    ]

    assert [response.status_code for response in responses[:12]] == [202] * 12
    assert responses[-1].status_code == 429
    assert responses[-1].json()["problem"]["code"] == "rate_limited"
    assert responses[-1].json()["problem"]["retryable"] is True


def test_unhandled_runtime_error_uses_typed_problem(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _user, _db = _install_authenticated_user()
    monkeypatch.setattr(
        kria_runtime,
        "submit_turn",
        AsyncMock(side_effect=ValueError("internal implementation detail")),
    )

    response = client.post(f"/creation-threads/{uuid.uuid4()}/turns", json=_body())

    assert response.status_code == 500
    assert response.json()["problem"]["code"] == "internal_error"
    assert response.json()["problem"]["message"] == (
        "Kria couldn't complete that request. Retry in a moment."
    )
    assert "implementation detail" not in response.text


def test_starlette_http_exception_keeps_status_in_typed_problem(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _user, _db = _install_authenticated_user()
    monkeypatch.setattr(
        kria_runtime,
        "submit_turn",
        AsyncMock(side_effect=StarletteHTTPException(503, "Runtime is draining")),
    )

    response = client.post(f"/creation-threads/{uuid.uuid4()}/turns", json=_body())

    assert response.status_code == 503
    assert response.json()["problem"]["code"] == "request_failed"
    assert response.json()["problem"]["message"] == "Runtime is draining"


def test_create_turn_runtime_flag_fails_closed_with_typed_problem(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _user, db = _install_authenticated_user()
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", False)
    submit = AsyncMock()
    monkeypatch.setattr(kria_runtime, "submit_turn", submit)

    response = client.post(
        f"/creation-threads/{uuid.uuid4()}/turns",
        headers={"X-Request-Id": "runtime-disabled-test"},
        json=_body(),
    )

    assert response.status_code == 404
    assert response.json() == {
        "problem": {
            "code": "kria_runtime_unavailable",
            "phase": "accept",
            "message": "Creation chat unavailable",
            "retryable": False,
            "recovery": "none",
            "trace_id": "runtime-disabled-test",
            "current_revision": None,
            "target": {},
        }
    }
    db.rollback.assert_awaited_once_with()
    submit.assert_not_awaited()


def test_create_turn_rejects_invalid_thread_uuid_with_typed_problem(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _user, db = _install_authenticated_user()
    submit = AsyncMock()
    monkeypatch.setattr(kria_runtime, "submit_turn", submit)

    response = client.post(
        "/creation-threads/not-a-uuid/turns",
        headers={"X-Request-Id": "invalid-thread-test"},
        json=_body(),
    )

    assert response.status_code == 404
    assert response.json()["problem"] == {
        "code": "thread_not_found",
        "phase": "accept",
        "message": "Creation thread not found",
        "retryable": False,
        "recovery": "none",
        "trace_id": "invalid-thread-test",
        "current_revision": None,
        "target": {},
    }
    db.rollback.assert_awaited_once_with()
    submit.assert_not_awaited()


def test_create_turn_returns_202_and_publishes_committed_turn(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, db = _install_authenticated_user()
    thread_id = uuid.uuid4()
    turn_id = uuid.uuid4()
    accepted = TurnAccepted(
        turn_id=str(turn_id),
        thread_revision=1,
        status="pending",
    )
    submit = AsyncMock(return_value=(accepted, True))
    publish = MagicMock()
    monkeypatch.setattr(kria_runtime, "submit_turn", submit)
    monkeypatch.setattr(kria_runtime, "_publish_turn", publish)

    response = client.post(f"/creation-threads/{thread_id}/turns", json=_body())

    assert response.status_code == 202
    assert response.json() == {
        "turn_id": str(turn_id),
        "thread_revision": 1,
        "status": "pending",
        "replayed": False,
    }
    assert submit.await_args.args == (db,)
    assert submit.await_args.kwargs["thread_id"] == thread_id
    assert submit.await_args.kwargs["creator_id"] == user.id
    assert submit.await_args.kwargs["body"].model_dump() == _body()
    publish.assert_called_once_with(str(turn_id))
    db.rollback.assert_not_awaited()


def test_create_turn_returns_typed_conflict_from_runtime_service(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _user, db = _install_authenticated_user()
    submit = AsyncMock(
        side_effect=RuntimeFailure(
            409,
            "thread_revision_stale",
            "The creation thread changed. Refresh and retry.",
            recovery="refresh_replan",
            current_revision=7,
        )
    )
    monkeypatch.setattr(kria_runtime, "submit_turn", submit)

    response = client.post(
        f"/creation-threads/{uuid.uuid4()}/turns",
        headers={"X-Request-Id": "stale-revision-test"},
        json=_body(expected_thread_revision=6),
    )

    assert response.status_code == 409
    assert response.json()["problem"] == {
        "code": "thread_revision_stale",
        "phase": "accept",
        "message": "The creation thread changed. Refresh and retry.",
        "retryable": False,
        "recovery": "refresh_replan",
        "trace_id": "stale-revision-test",
        "current_revision": 7,
        "target": {},
    }
    db.rollback.assert_awaited_once_with()


def test_create_turn_keeps_202_recovery_ledger_when_broker_publish_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _user, db = _install_authenticated_user()
    turn_id = uuid.uuid4()
    accepted = TurnAccepted(
        turn_id=str(turn_id),
        thread_revision=3,
        status="pending",
    )
    submit = AsyncMock(return_value=(accepted, True))
    publish = MagicMock(side_effect=RuntimeError("broker unavailable"))
    logger = MagicMock()
    monkeypatch.setattr(kria_runtime, "submit_turn", submit)
    monkeypatch.setattr(kria_runtime, "_publish_turn", publish)
    monkeypatch.setattr(kria_runtime, "log", logger)

    response = client.post(f"/creation-threads/{uuid.uuid4()}/turns", json=_body())

    assert response.status_code == 202
    assert response.json()["turn_id"] == str(turn_id)
    assert response.json()["status"] == "pending"
    publish.assert_called_once_with(str(turn_id))
    logger.error.assert_called_once_with(
        "kria_turn_publish_failed",
        turn_id=str(turn_id),
        error_class="RuntimeError",
    )
    db.rollback.assert_not_awaited()


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (
            SubmitTurnBody,
            {
                "message": "   ",
                "client_event_id": "event-1",
                "expected_thread_revision": 0,
            },
        ),
        (
            SubmitTurnBody,
            {
                "message": "Make this tighter",
                "client_event_id": "../event-1",
                "expected_thread_revision": 0,
            },
        ),
        (TurnCancelBody, {"expected_thread_revision": -1}),
        (
            ApprovalDecisionBody,
            {
                "expected_thread_revision": 0,
                "expected_draft_revision": 0,
                "expected_approval_fingerprint": "A" * 64,
            },
        ),
    ],
)
def test_runtime_request_bodies_reject_unsafe_boundaries(model, payload) -> None:  # noqa: ANN001
    with pytest.raises(ValueError):
        model.model_validate(payload)


def test_get_delta_delegates_cursor_and_returns_runtime_projection(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, db = _install_authenticated_user()
    thread_id = uuid.uuid4()
    projection = ThreadDeltaOut(
        thread_id=str(thread_id),
        runtime_version=2,
        status="active",
        thread_revision=9,
        events=[],
        after_sequence=4,
        next_after_sequence=4,
        has_more=False,
    )
    read = AsyncMock(return_value=projection)
    monkeypatch.setattr(kria_runtime, "read_delta", read)

    response = client.get(f"/creation-threads/{thread_id}/delta?after_sequence=4&limit=25")

    assert response.status_code == 200
    assert response.json() == projection.model_dump(mode="json")
    assert read.await_args.args == (db,)
    assert read.await_args.kwargs == {
        "thread_id": thread_id,
        "creator_id": user.id,
        "after_sequence": 4,
        "before_sequence": None,
        "limit": 25,
    }
    db.rollback.assert_not_awaited()


def test_get_delta_is_available_on_approved_thread_cursor_contract(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, db = _install_authenticated_user()
    thread_id = uuid.uuid4()
    projection = ThreadDeltaOut(
        thread_id=str(thread_id),
        runtime_version=2,
        status="active",
        thread_revision=9,
        events=[],
        after_sequence=4,
        next_after_sequence=4,
        has_more=False,
    )
    read = AsyncMock(return_value=projection)
    monkeypatch.setattr(creation_threads, "read_delta", read)

    response = client.get(f"/creation-threads/{thread_id}?after_sequence=4&limit=25")

    assert response.status_code == 200
    assert response.json() == projection.model_dump(mode="json")
    assert read.await_args.args == (db,)
    assert read.await_args.kwargs == {
        "thread_id": thread_id,
        "creator_id": user.id,
        "after_sequence": 4,
        "before_sequence": None,
        "limit": 25,
    }


def test_runtime_v2_no_cursor_bootstraps_bounded_newest_page(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, db = _install_authenticated_user()
    thread_id = uuid.uuid4()
    projection = ThreadDeltaOut(
        thread_id=str(thread_id),
        runtime_version=2,
        status="active",
        thread_revision=9,
        events=[],
        after_sequence=-1,
        next_after_sequence=-1,
        has_more=False,
    )
    monkeypatch.setattr(
        creation_threads,
        "_load",
        AsyncMock(return_value=SimpleNamespace(id=thread_id, runtime_version=2)),
    )
    read = AsyncMock(return_value=projection)
    monkeypatch.setattr(creation_threads, "read_delta", read)

    response = client.get(f"/creation-threads/{thread_id}")

    assert response.status_code == 200
    assert response.json() == projection.model_dump(mode="json")
    db.rollback.assert_awaited_once_with()
    assert read.await_args.kwargs == {
        "thread_id": thread_id,
        "creator_id": user.id,
        "after_sequence": -1,
        "limit": 100,
    }


def test_cursor_query_validation_uses_kria_problem_envelope(client: TestClient) -> None:
    _install_authenticated_user()

    response = client.get(
        f"/creation-threads/{uuid.uuid4()}?after_sequence=invalid",
        headers={"X-Request-Id": "cursor-validation-test"},
    )

    assert response.status_code == 422
    assert response.json()["problem"] == {
        "code": "request_invalid",
        "phase": "accept",
        "message": "Check the request and try again.",
        "retryable": False,
        "recovery": "manual",
        "trace_id": "cursor-validation-test",
        "current_revision": None,
        "target": {},
    }


def test_get_delta_returns_typed_failure_and_rolls_back(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _user, db = _install_authenticated_user()
    read = AsyncMock(
        side_effect=RuntimeFailure(
            409,
            "runtime_version_required",
            "This project uses the original creation experience.",
            recovery="manual",
            current_revision=3,
        )
    )
    monkeypatch.setattr(kria_runtime, "read_delta", read)

    response = client.get(
        f"/creation-threads/{uuid.uuid4()}/delta",
        headers={"X-Request-Id": "delta-failure-test"},
    )

    assert response.status_code == 409
    assert response.json()["problem"] == {
        "code": "runtime_version_required",
        "phase": "accept",
        "message": "This project uses the original creation experience.",
        "retryable": False,
        "recovery": "manual",
        "trace_id": "delta-failure-test",
        "current_revision": 3,
        "target": {},
    }
    db.rollback.assert_awaited_once_with()


def test_cancel_turn_delegates_and_publishes_promoted_successor(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, db = _install_authenticated_user()
    thread_id = uuid.uuid4()
    turn_id = uuid.uuid4()
    successor_id = str(uuid.uuid4())
    cancelled = TurnCancelled(
        turn_id=str(turn_id),
        thread_revision=7,
        status="cancelled",
        approval_ids=[str(uuid.uuid4())],
    )
    cancel = AsyncMock(return_value=(cancelled, successor_id))
    publish = MagicMock()
    monkeypatch.setattr(kria_runtime, "cancel_turn", cancel)
    monkeypatch.setattr(kria_runtime, "_publish_turn", publish)

    response = client.post(
        f"/creation-threads/{thread_id}/turns/{turn_id}/cancel",
        json={"expected_thread_revision": 6},
    )

    assert response.status_code == 200
    assert response.json() == cancelled.model_dump(mode="json")
    assert cancel.await_args.args == (db,)
    assert cancel.await_args.kwargs == {
        "thread_id": thread_id,
        "turn_id": turn_id,
        "creator_id": user.id,
        "expected_thread_revision": 6,
    }
    publish.assert_called_once_with(successor_id)
    db.rollback.assert_not_awaited()


def test_cancel_turn_returns_typed_failure_and_rolls_back(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _user, db = _install_authenticated_user()
    cancel = AsyncMock(
        side_effect=RuntimeFailure(
            409,
            "turn_not_cancellable",
            "Kria has already committed work for this turn.",
            recovery="manual",
            current_revision=8,
        )
    )
    monkeypatch.setattr(kria_runtime, "cancel_turn", cancel)

    response = client.post(
        f"/creation-threads/{uuid.uuid4()}/turns/{uuid.uuid4()}/cancel",
        headers={"X-Request-Id": "cancel-failure-test"},
        json={"expected_thread_revision": 8},
    )

    assert response.status_code == 409
    assert response.json()["problem"]["code"] == "turn_not_cancellable"
    assert response.json()["problem"]["trace_id"] == "cancel-failure-test"
    assert response.json()["problem"]["recovery"] == "manual"
    db.rollback.assert_awaited_once_with()


def test_decide_approval_delegates_and_publishes_denied_turn_successor(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, db = _install_authenticated_user()
    thread_id = uuid.uuid4()
    approval_id = uuid.uuid4()
    turn_id = uuid.uuid4()
    successor_id = str(uuid.uuid4())
    decision = ApprovalDecisionOut(
        approval_id=str(approval_id),
        turn_id=str(turn_id),
        status="denied",
        thread_revision=10,
        render_dispatched=False,
    )
    decide = AsyncMock(return_value=(decision, successor_id))
    publish = MagicMock()
    monkeypatch.setattr(kria_runtime, "decide_approval", decide)
    monkeypatch.setattr(kria_runtime, "_publish_turn", publish)
    body = {
        "expected_thread_revision": 9,
        "expected_draft_revision": 4,
        "expected_approval_fingerprint": "a" * 64,
    }

    response = client.post(
        f"/creation-threads/{thread_id}/approvals/{approval_id}/deny",
        json=body,
    )

    assert response.status_code == 200
    assert response.json() == decision.model_dump(mode="json")
    assert decide.await_args.args == (db,)
    assert decide.await_args.kwargs["thread_id"] == thread_id
    assert decide.await_args.kwargs["approval_id"] == approval_id
    assert decide.await_args.kwargs["creator_id"] == user.id
    assert decide.await_args.kwargs["decision"] == "deny"
    assert decide.await_args.kwargs["body"].model_dump() == body
    publish.assert_called_once_with(successor_id)
    db.rollback.assert_not_awaited()


def test_approved_decision_publishes_exact_approval_for_durable_execution(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _user, db = _install_authenticated_user()
    thread_id = uuid.uuid4()
    approval_id = uuid.uuid4()
    turn_id = uuid.uuid4()
    decision = ApprovalDecisionOut(
        approval_id=str(approval_id),
        turn_id=str(turn_id),
        status="approved",
        thread_revision=10,
        render_dispatched=False,
    )
    monkeypatch.setattr(
        kria_runtime,
        "decide_approval",
        AsyncMock(return_value=(decision, None)),
    )
    publish_approval = MagicMock()
    monkeypatch.setattr(kria_runtime, "_publish_approval", publish_approval)

    response = client.post(
        f"/creation-threads/{thread_id}/approvals/{approval_id}/approve",
        json={
            "expected_thread_revision": 9,
            "expected_draft_revision": 4,
            "expected_approval_fingerprint": "e" * 64,
        },
    )

    assert response.status_code == 200
    assert response.json()["render_dispatched"] is False
    publish_approval.assert_called_once_with(str(approval_id))
    db.rollback.assert_not_awaited()


def test_approved_decision_keeps_consent_ledger_when_publish_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _user, db = _install_authenticated_user()
    approval_id = uuid.uuid4()
    decision = ApprovalDecisionOut(
        approval_id=str(approval_id),
        turn_id=str(uuid.uuid4()),
        status="approved",
        thread_revision=10,
        render_dispatched=False,
    )
    monkeypatch.setattr(
        kria_runtime,
        "decide_approval",
        AsyncMock(return_value=(decision, None)),
    )
    monkeypatch.setattr(
        kria_runtime,
        "_publish_approval",
        MagicMock(side_effect=RuntimeError("broker unavailable")),
    )
    logger = MagicMock()
    monkeypatch.setattr(kria_runtime, "log", logger)

    response = client.post(
        f"/creation-threads/{uuid.uuid4()}/approvals/{approval_id}/approve",
        json={
            "expected_thread_revision": 9,
            "expected_draft_revision": 4,
            "expected_approval_fingerprint": "f" * 64,
        },
    )

    assert response.status_code == 200
    logger.error.assert_called_once_with(
        "kria_approval_publish_failed",
        approval_id=str(approval_id),
        error_class="RuntimeError",
    )
    db.rollback.assert_not_awaited()


def test_decide_approval_returns_typed_failure_and_rolls_back(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _user, db = _install_authenticated_user()
    decide = AsyncMock(
        side_effect=RuntimeFailure(
            409,
            "approval_expired",
            "This approval expired. Ask Kria to prepare it again.",
            phase="approval",
            recovery="refresh_replan",
            current_revision=11,
        )
    )
    monkeypatch.setattr(kria_runtime, "decide_approval", decide)

    response = client.post(
        f"/creation-threads/{uuid.uuid4()}/approvals/{uuid.uuid4()}/approve",
        headers={"X-Request-Id": "approval-failure-test"},
        json={
            "expected_thread_revision": 11,
            "expected_draft_revision": 4,
            "expected_approval_fingerprint": "b" * 64,
        },
    )

    assert response.status_code == 409
    assert response.json()["problem"]["code"] == "approval_expired"
    assert response.json()["problem"]["phase"] == "approval"
    assert response.json()["problem"]["trace_id"] == "approval-failure-test"
    assert response.json()["problem"]["recovery"] == "refresh_replan"
    db.rollback.assert_awaited_once_with()


@pytest.mark.parametrize("adapter", ["cancel", "approval"])
def test_successor_publish_failure_keeps_committed_adapter_response_recoverable(
    adapter: str,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _user, db = _install_authenticated_user()
    thread_id = uuid.uuid4()
    turn_id = uuid.uuid4()
    successor_id = str(uuid.uuid4())
    publish = MagicMock(side_effect=RuntimeError("broker unavailable"))
    logger = MagicMock()
    monkeypatch.setattr(kria_runtime, "_publish_turn", publish)
    monkeypatch.setattr(kria_runtime, "log", logger)

    if adapter == "cancel":
        endpoint = f"/creation-threads/{thread_id}/turns/{turn_id}/cancel"
        body = {"expected_thread_revision": 6}
        monkeypatch.setattr(
            kria_runtime,
            "cancel_turn",
            AsyncMock(
                return_value=(
                    TurnCancelled(
                        turn_id=str(turn_id),
                        thread_revision=7,
                        status="cancelled",
                    ),
                    successor_id,
                )
            ),
        )
    else:
        approval_id = uuid.uuid4()
        endpoint = f"/creation-threads/{thread_id}/approvals/{approval_id}/deny"
        body = {
            "expected_thread_revision": 9,
            "expected_draft_revision": 4,
            "expected_approval_fingerprint": "c" * 64,
        }
        monkeypatch.setattr(
            kria_runtime,
            "decide_approval",
            AsyncMock(
                return_value=(
                    ApprovalDecisionOut(
                        approval_id=str(approval_id),
                        turn_id=str(turn_id),
                        status="denied",
                        thread_revision=10,
                        render_dispatched=False,
                    ),
                    successor_id,
                )
            ),
        )

    response = client.post(endpoint, json=body)

    assert response.status_code == 200
    publish.assert_called_once_with(successor_id)
    logger.error.assert_called_once_with(
        "kria_successor_publish_failed",
        turn_id=successor_id,
        error_class="RuntimeError",
    )
    db.rollback.assert_not_awaited()


def test_get_approval_exposes_server_fingerprint(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, db = _install_authenticated_user()
    thread_id = uuid.uuid4()
    approval_id = uuid.uuid4()
    turn_id = uuid.uuid4()
    expires_at = "2026-09-06T18:00:00Z"
    read = AsyncMock(
        return_value=ApprovalSnapshotOut(
            approval_id=str(approval_id),
            turn_id=str(turn_id),
            draft_id=str(uuid.uuid4()),
            draft_revision=4,
            status="pending",
            consequence_summary="Render the tighter matcha cut.",
            cost_summary="One render",
            expires_at=expires_at,
            approval_fingerprint="d" * 64,
        )
    )
    monkeypatch.setattr(kria_runtime, "read_approval", read)

    response = client.get(f"/creation-threads/{thread_id}/approvals/{approval_id}")

    assert response.status_code == 200
    assert response.json()["approval_fingerprint"] == "d" * 64
    read.assert_awaited_once_with(
        db,
        thread_id=thread_id,
        approval_id=approval_id,
        creator_id=user.id,
    )


def _draft_snapshot() -> DraftSnapshotOut:
    return DraftSnapshotOut(
        draft_id=str(uuid.uuid4()),
        item_id=str(uuid.uuid4()),
        variant_key="initial",
        draft_revision=2,
        snapshot_hash="e" * 64,
        etag='"' + "e" * 64 + '"',
        snapshot={
            "schema_version": 2,
            "kind": "initial",
            "intent": "A concise matcha launch",
            "edit_format": "montage",
            "editor_payload": None,
            "changes": ["Open on the whisk"],
        },
        can_undo=True,
        created_at="2026-09-06T18:00:00Z",
    )


def test_get_draft_bootstraps_authoritative_snapshot(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, db = _install_authenticated_user()
    thread_id = uuid.uuid4()
    read = AsyncMock(return_value=_draft_snapshot())
    monkeypatch.setattr(kria_runtime, "read_or_bootstrap_draft", read)

    response = client.get(f"/creation-threads/{thread_id}/draft")

    assert response.status_code == 200
    assert response.json()["draft_revision"] == 2
    read.assert_awaited_once_with(db, thread_id=thread_id, creator_id=user.id)


def test_put_draft_requires_etag(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _user, db = _install_authenticated_user()
    write = AsyncMock()
    monkeypatch.setattr(kria_runtime, "write_draft", write)

    response = client.put(
        f"/creation-threads/{uuid.uuid4()}/draft",
        json={"expected_draft_revision": 2, "snapshot": _draft_snapshot().snapshot},
    )

    assert response.status_code == 428
    assert response.json()["problem"]["code"] == "draft_precondition_required"
    write.assert_not_awaited()
    db.rollback.assert_awaited_once_with()


def test_put_and_undo_draft_forward_cas_contract(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, db = _install_authenticated_user()
    thread_id = uuid.uuid4()
    draft = _draft_snapshot()
    write = AsyncMock(return_value=draft)
    undo = AsyncMock(return_value=(draft, None))
    monkeypatch.setattr(kria_runtime, "write_draft", write)
    monkeypatch.setattr(kria_runtime, "undo_draft", undo)

    put_response = client.put(
        f"/creation-threads/{thread_id}/draft",
        headers={"If-Match": draft.etag},
        json={"expected_draft_revision": 1, "snapshot": draft.snapshot},
    )
    undo_response = client.post(
        f"/creation-threads/{thread_id}/draft/undo",
        json={"expected_draft_revision": 2},
    )

    assert put_response.status_code == 200
    assert undo_response.status_code == 200
    write.assert_awaited_once_with(
        db,
        thread_id=thread_id,
        creator_id=user.id,
        expected_revision=1,
        expected_etag=draft.etag,
        snapshot=draft.snapshot,
    )
    undo.assert_awaited_once_with(
        db,
        thread_id=thread_id,
        creator_id=user.id,
        expected_revision=2,
    )
