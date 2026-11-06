"""Strict request recovery through HTTP with counting fake paid work; all sockets denied."""

import copy
import socket
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.auth import get_current_user
from app.config import settings
from app.database import get_db
from app.main import app
from app.routes import plan_items
from app.routes import slide_post_ai_requests as route
from app.services.slide_post_ai_requests import AIReceiptConflict, input_digest
from app.services.slide_post_chat_edit import SlidePostChatEditResponse
from tests.routes.test_slide_post_chat_edit_route import (
    _draft,
    _install,
    _reset,  # noqa: F401 - pytest fixture
)


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def rig(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("network forbidden")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(settings, "slide_post_ai_recovery_enabled", True)
    ids = [uuid.uuid4()]
    item, _ = _install(monkeypatch, _draft(ids, version=1), ids)
    user = SimpleNamespace(id=uuid.uuid4())
    project = SimpleNamespace(
        id=uuid.uuid4(), creator_id=user.id, active_plan_item_id=item.id, status="active"
    )
    app.dependency_overrides[get_current_user] = lambda: user
    monkeypatch.setattr(
        plan_items, "_slide_post_thread_for_item", AsyncMock(side_effect=lambda *args: project)
    )
    monkeypatch.setattr(
        plan_items,
        "_load_owned_item_context",
        AsyncMock(return_value=(item, SimpleNamespace(), None)),
    )
    monkeypatch.setattr(
        "app.services.slide_post_ai_authority.authority_snapshot",
        AsyncMock(return_value={"original": True}),
    )
    monkeypatch.setattr("app.services.slide_post_ai_delivery.publish_due", AsyncMock())
    rows = {}
    db = AsyncMock()
    db.get.side_effect = lambda _model, key, **kw: rows.get(key)
    app.dependency_overrides[get_db] = lambda: db
    calls = []

    class Store:
        def __init__(self, _db):
            pass

        async def reserve(
            self,
            *,
            request_id,
            owner_id,
            item_id,
            project_id,
            kind,
            body,
            prepared_input=None,
            authority_snapshot=None,
        ):
            digest = input_digest(kind, body)
            row = rows.get(request_id)
            if row is not None:
                if (row.owner_id, row.item_id, row.project_id, row.kind, row.input_digest) != (
                    owner_id,
                    item_id,
                    project_id,
                    kind,
                    digest,
                ):
                    raise AIReceiptConflict()
                return row
            row = SimpleNamespace(
                id=request_id,
                owner_id=owner_id,
                item_id=item_id,
                project_id=project_id,
                kind=kind,
                input_digest=digest,
                request_input=copy.deepcopy(body),
                status="pending",
                claim_id=None,
                claimed_at=None,
                result=None,
                resolved_input=None,
                prepared_input=copy.deepcopy(prepared_input),
                authority_snapshot=copy.deepcopy(authority_snapshot),
            )
            rows[request_id] = row
            return row

        async def claim(self, request_id, snapshot):
            row = rows[request_id]
            if row.status != "pending":
                return None
            row.status = "running"
            row.claim_id = uuid.uuid4()
            row.claimed_at = datetime.now(UTC)
            row.resolved_input = copy.deepcopy(snapshot)
            return row.claim_id

        async def complete(self, request_id, claim_id, result):
            row = rows[request_id]
            if row.claim_id != claim_id:
                return False
            row.status = "completed"
            row.result = copy.deepcopy(result)
            return True

        async def reject(self, request_id, message):
            row = rows[request_id]
            if row.status == "pending":
                row.status = "rejected"
                row.result = {"message": message}

        async def unknown(self, request_id, claim_id):
            row = rows[request_id]
            if row.claim_id == claim_id and row.status == "running":
                row.status = "unknown"

    monkeypatch.setattr(route, "SQLAIRequestStore", Store)
    monkeypatch.setattr("app.services.slide_post_ai_delivery.SQLAIRequestStore", Store)

    async def execute_fake(kind, prepared):
        from app.agents._runtime import RunContext

        calls.append(RunContext(**prepared["snapshot"]["context"]))
        return {"outcome": "no_effect", "reply": "original result", "base_version": 1}

    monkeypatch.setattr("app.services.slide_post_ai_delivery.execute_prepared", execute_fake)

    async def fake_service(**kwargs):
        async def fake_paid():
            calls.append(kwargs["run_context"])
            return SlidePostChatEditResponse(
                outcome="no_effect", reply="original result", base_version=1
            )

        gate = kwargs.get("paid_execution")
        if gate:
            from app.services.slide_post_ai_prepared import prepared_context

            return await gate.run(
                {
                    "agent_input": {"utterance": kwargs["message"], "variant_snapshot": {}},
                    "context": prepared_context(kwargs["run_context"]),
                },
                fake_paid,
            )
        return await fake_paid()

    monkeypatch.setattr(plan_items, "run_slide_post_chat_edit", fake_service)
    return SimpleNamespace(
        item=item, user=user, project=project, rows=rows, calls=calls, store=Store(db)
    )


def body(rig, request_id=None):
    return {
        "client_request_id": str(request_id or uuid.uuid4()),
        "project_id": str(rig.project.id),
        "kind": "chat_edit",
        "input": {"message": "caption", "expected_version": 1},
    }


def test_lost_response_retry_recovers_identical_result_once(client, rig):
    payload = body(rig)
    path = f"/plan-items/{rig.item.id}/slide-post/ai-requests"
    first = client.post(path, json=payload)
    assert first.status_code == 200, first.text
    # Discard the response; a recreated client first observes the durable receipt.
    observed = client.get(path + "/" + payload["client_request_id"])
    repeated = client.post(path, json=payload)
    assert observed.json() == repeated.json() == first.json()
    assert observed.json()["result"]["reply"] == "original result"
    assert len(rig.calls) == 1
    assert rig.calls[0].request_id_authoritative is True


def test_changed_input_or_project_or_owner_cannot_reuse_result(client, rig):
    payload = body(rig)
    path = f"/plan-items/{rig.item.id}/slide-post/ai-requests"
    assert client.post(path, json=payload).status_code == 200
    altered = copy.deepcopy(payload)
    altered["input"]["message"] = "other"
    assert client.post(path, json=altered).status_code == 409
    rig.project.id = uuid.uuid4()
    assert client.get(path + "/" + payload["client_request_id"]).status_code == 404
    assert client.post(path, json=payload).status_code == 409
    rig.user.id = uuid.uuid4()
    assert client.get(path + "/" + payload["client_request_id"]).status_code == 404
    assert len(rig.calls) == 1


def test_unknown_provider_outcome_never_calls_again(client, rig, monkeypatch):
    async def uncertain(kind, prepared):
        rig.calls.append("unknown")
        raise TimeoutError("lost provider result")

    monkeypatch.setattr("app.services.slide_post_ai_delivery.execute_prepared", uncertain)
    payload = body(rig)
    path = f"/plan-items/{rig.item.id}/slide-post/ai-requests"
    first = client.post(path, json=payload)
    assert first.status_code == 200 and first.json()["status"] == "unknown"
    assert client.post(path, json=payload).json()["status"] == "unknown"
    assert client.get(path + "/" + payload["client_request_id"]).json()["status"] == "unknown"
    assert len(rig.calls) == 1


def test_recovery_disabled_has_no_legacy_fallback_or_paid_work(client, rig, monkeypatch):
    monkeypatch.setattr(settings, "slide_post_ai_recovery_enabled", False)
    payload = body(rig)
    assert (
        client.post(f"/plan-items/{rig.item.id}/slide-post/ai-requests", json=payload).status_code
        == 404
    )
    assert rig.calls == [] and rig.rows == {}


def test_legacy_fresh_uuid_retry_reproduces_duplicate_paid_boundary(client, rig):
    # Baseline evidence: legacy compatibility remains unchanged.
    # Native must never use this endpoint for recovery.
    path = f"/plan-items/{rig.item.id}/slide-post/chat-edit"
    for _ in range(2):
        assert (
            client.post(
                path, json={"message": "caption", "client_request_id": str(uuid.uuid4())}
            ).status_code
            == 200
        )
    assert len(rig.calls) == 2


def test_stale_strict_chat_is_rejected_without_paid_work(client, rig):
    payload = body(rig)
    payload["input"]["expected_version"] = 0
    response = client.post(f"/plan-items/{rig.item.id}/slide-post/ai-requests", json=payload)
    assert response.status_code == 200 and response.json()["status"] == "rejected", response.text
    assert rig.calls == []
