# ruff: noqa: F811
"""Real PostgreSQL claims and HTTP receipts; model calls are counting parser fakes."""

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.database import sync_session
from app.models import CreationThread, PlanItem, SlidePostAIRequest
from app.services.slide_post_ai_requests import SQLAIRequestStore
from tests.routes.test_slide_post_binding_save_postgres import (  # noqa: F401
    _flags,
    _ParsedOps,
    client,
    seeded,
)


@pytest.fixture
def paid(monkeypatch):
    monkeypatch.setattr(settings, "slide_post_ai_recovery_enabled", True)
    calls = []
    lock = Lock()

    class Fake(_ParsedOps):
        def run(self, value, ctx=None):
            with lock:
                calls.append(ctx.request_id)
            return super().run(value, ctx)

    monkeypatch.setattr("app.agents._model_client.default_client", lambda: object())
    monkeypatch.setattr("app.services.slide_post_chat_edit.EditCopilotAgent", Fake)
    return calls


def payload(seeded, key=None, version=1):
    key = key or uuid.uuid4()
    return {
        "client_request_id": str(key).upper(),
        "project_id": str(seeded.thread_id),
        "kind": "chat_edit",
        "input": {
            "client_request_id": str(key).upper(),
            "message": "Keep these photos",
            "expected_version": version,
        },
    }


def path(seeded):
    return f"/plan-items/{seeded.item_id}/slide-post/ai-requests"


def test_committed_lost_response_replays_after_newer_save_with_one_paid_call(client, seeded, paid):
    body = payload(seeded)
    first = client.post(path(seeded), json=body)
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "completed", first.text
    with sync_session() as db:
        item = db.get(PlanItem, seeded.item_id)
        item.slide_post = {**item.slide_post, "version": 2, "caption": "newer"}
        db.commit()
    observed = client.get(path(seeded) + "/" + body["client_request_id"])
    replayed = client.post(path(seeded), json=body)
    assert observed.json() == replayed.json() == first.json()
    assert len(paid) == 1


def test_stale_input_is_rejected_before_paid_claim(client, seeded, paid):
    body = payload(seeded, version=0)
    reply = client.post(path(seeded), json=body)
    assert reply.status_code == 200 and reply.json()["status"] == "rejected", reply.text
    assert client.post(path(seeded), json=body).json()["status"] == "rejected"
    with sync_session() as db:
        row = db.get(SlidePostAIRequest, uuid.UUID(body["client_request_id"]))
        assert row.claim_id is None and row.resolved_input is None
    assert paid == []


def test_two_sessions_claim_once(client, seeded, paid):
    body = payload(seeded)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: client.post(path(seeded), json=body), range(2)))
    assert [r.status_code for r in responses] == [200, 200], [r.text for r in responses]
    assert len(paid) == 1
    assert (
        client.get(path(seeded) + "/" + body["client_request_id"]).json()["status"] == "completed"
    )


def test_pending_receipt_resumes_once_and_unknown_never_reexecutes(client, seeded, paid):
    from app.services.slide_post_ai_requests import input_digest

    body = payload(seeded)
    key = uuid.UUID(body["client_request_id"])
    with sync_session() as db:
        db.add(
            SlidePostAIRequest(
                id=key,
                owner_id=seeded.user_id,
                item_id=seeded.item_id,
                project_id=seeded.thread_id,
                kind="chat_edit",
                input_digest=input_digest("chat_edit", body["input"]),
                request_input=body["input"],
                status="pending",
            )
        )
        db.commit()
    assert client.get(path(seeded) + "/" + str(key)).json()["status"] == "pending"
    assert paid == []
    assert client.post(path(seeded), json=body).json()["status"] == "completed"
    with sync_session() as db:
        row = db.get(SlidePostAIRequest, key)
        row.status = "unknown"
        row.result = None
        token = row.claim_id
        db.commit()
    assert client.post(path(seeded), json=body).json()["status"] == "unknown"
    assert len(paid) == 1

    async def late_result():
        engine = create_async_engine(settings.asyncpg_database_url, poolclass=NullPool)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as db:
                store = SQLAIRequestStore(db)
                assert not await store.complete(key, uuid.uuid4(), {"wrong": True})
                assert await store.complete(key, token, {"verified": "late result"})
        finally:
            await engine.dispose()

    asyncio.run(late_result())
    assert client.get(path(seeded) + "/" + str(key)).json()["result"] == {"verified": "late result"}


def test_wrong_echo_or_replaced_project_never_exposes_or_repeats_result(client, seeded, paid):
    body = payload(seeded)
    assert client.post(path(seeded), json=body).status_code == 200
    changed = {**body, "input": {**body["input"], "message": "different"}}
    assert client.post(path(seeded), json=changed).status_code == 409
    changed = {**body, "input": {**body["input"], "client_request_id": str(uuid.uuid4())}}
    assert client.post(path(seeded), json=changed).status_code == 409
    with sync_session() as db:
        db.get(CreationThread, seeded.thread_id).active_plan_item_id = seeded.other_item_id
        db.commit()
    assert client.get(path(seeded) + "/" + body["client_request_id"]).status_code == 404
    assert client.post(path(seeded), json=body).status_code == 409
    assert len(paid) == 1


@pytest.mark.parametrize("change", ["save", "ownership"])
def test_change_after_reservation_is_revalidated_before_paid_claim(
    client, seeded, paid, monkeypatch, change
):
    original = SQLAIRequestStore.reserve

    async def reserve_then_change(self, **kwargs):
        row = await original(self, **kwargs)
        with sync_session() as db:
            item = db.get(PlanItem, seeded.item_id)
            if change == "save":
                item.slide_post = {**item.slide_post, "version": 2, "caption": "separate session"}
            else:
                # Existing other authenticated user; no synthetic invalid FK owner.
                from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

                _, other_thread_id, _ = _seed_runtime_project()
                other_item_id = db.get(CreationThread, other_thread_id).active_plan_item_id
                item.content_plan_id = db.get(PlanItem, other_item_id).content_plan_id
                item.position = 99
            db.commit()
        return row

    monkeypatch.setattr(SQLAIRequestStore, "reserve", reserve_then_change)
    response = client.post(path(seeded), json=payload(seeded))
    assert response.status_code in (200, 404), response.text
    if response.status_code == 200:
        assert response.json()["status"] == "rejected", response.text
    assert paid == []


def test_busy_thread_keeps_pending_and_releases_claim_locks_without_deadlock(client, seeded, paid):
    from sqlalchemy import select

    body = payload(seeded)
    with sync_session() as blocker:
        blocker.execute(
            select(CreationThread).where(CreationThread.id == seeded.thread_id).with_for_update()
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            response = pool.submit(lambda: client.post(path(seeded), json=body)).result(timeout=5)
        assert response.status_code == 200 and response.json()["status"] == "pending", response.text
        assert paid == []
        # The failed savepoint released its Item lock; the blocker can finish its mutation.
        blocker.execute(
            select(PlanItem).where(PlanItem.id == seeded.item_id).with_for_update(nowait=True)
        )
        blocker.rollback()
    response = client.post(path(seeded), json=body)
    assert response.status_code == 200 and response.json()["status"] == "completed", response.text
    assert len(paid) == 1


def test_real_strict_service_preserves_unknown_instead_of_legacy_fallback(
    client, seeded, paid, monkeypatch
):
    from app.agents._runtime import ProviderOutcomeUnknownError

    class Unknown:
        def __init__(self, _client):
            pass

        def run(self, _value, ctx=None):
            paid.append(ctx.request_id)
            raise ProviderOutcomeUnknownError("provider result unverified")

    monkeypatch.setattr("app.services.slide_post_chat_edit.EditCopilotAgent", Unknown)
    body = payload(seeded)
    response = client.post(path(seeded), json=body)
    assert response.status_code == 200 and response.json()["status"] == "unknown", response.text
    assert response.json()["result"] is None
    assert client.post(path(seeded), json=body).json()["status"] == "unknown"
    assert len(paid) == 1


def test_rollout_off_preserves_existing_result_observation(client, seeded, paid, monkeypatch):
    body = payload(seeded)
    first = client.post(path(seeded), json=body)
    assert first.status_code == 200 and first.json()["status"] == "completed", first.text
    monkeypatch.setattr(settings, "slide_post_ai_recovery_enabled", False)
    assert client.get(path(seeded) + "/" + body["client_request_id"]).json() == first.json()
    assert client.post(path(seeded), json=body).json() == first.json()
    assert client.post(path(seeded), json=payload(seeded)).status_code == 404
    assert len(paid) == 1
