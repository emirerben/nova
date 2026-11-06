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
    monkeypatch.setattr(
        "app.tasks.slide_post_ai_delivery.execute_slide_post_ai_request.delay", lambda *_: None
    )
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


def test_pending_receipt_resumes_once_and_unknown_never_reexecutes(
    client, seeded, paid, monkeypatch
):
    body, key = _pending(client, seeded, monkeypatch)
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
        with sync_session() as observer:
            pending = observer.get(SlidePostAIRequest, uuid.UUID(body["client_request_id"]))
            assert getattr(pending, "prepared_input", None) is not None
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


def test_legacy_pending_cannot_reconstruct_from_new_metadata(client, seeded, paid):
    from app.services.slide_post_ai_requests import input_digest

    body = payload(seeded)
    with sync_session() as db:
        db.add(
            SlidePostAIRequest(
                id=uuid.UUID(body["client_request_id"]),
                owner_id=seeded.user_id,
                item_id=seeded.item_id,
                project_id=seeded.thread_id,
                kind="chat_edit",
                input_digest=input_digest("chat_edit", body["input"]),
                request_input=body["input"],
                status="pending",
            )
        )
        item = db.get(PlanItem, seeded.item_id)
        item.slide_post = {**item.slide_post, "caption": "Changed without a version bump"}
        db.commit()
    observed = client.get(path(seeded) + "/" + body["client_request_id"])
    assert observed.json()["status"] == "unknown"
    assert paid == []
    resumed = client.post(path(seeded), json=body)
    assert resumed.json()["status"] == "unknown", resumed.text
    assert paid == []

    with sync_session() as db:
        row = db.get(SlidePostAIRequest, uuid.UUID(body["client_request_id"]))
        assert row.status == "pending" and row.result is None and row.claim_id is None


def _pending(client, seeded, monkeypatch):
    """Crash-equivalent after durable admission, before inline claim/publication."""

    async def paused(*args, **kwargs):
        pass

    with monkeypatch.context() as patch:
        patch.setattr("app.services.slide_post_ai_delivery.deliver", paused)
        patch.setattr("app.services.slide_post_ai_delivery.publish_due", paused)
        body = payload(seeded)
        response = client.post(path(seeded), json=body)
    assert response.status_code == 200 and response.json()["status"] == "pending", response.text
    assert response.json()["delivery_version"] == 1
    return body, uuid.UUID(body["client_request_id"])


def _with_async_db(operation):
    async def run():
        engine = create_async_engine(settings.asyncpg_database_url, poolclass=NullPool)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as db:
                return await operation(db)
        finally:
            await engine.dispose()

    return asyncio.run(run())


def test_lost_enqueue_sweep_reserves_before_publish_and_duplicate_delivery_claims_once(
    client, seeded, paid, monkeypatch
):
    from datetime import UTC, datetime, timedelta

    from app.services.slide_post_ai_delivery import deliver, publish_due

    _, key = _pending(client, seeded, monkeypatch)
    attempts = []

    def lost_enqueue(identifier):
        with sync_session() as db:
            row = db.get(SlidePostAIRequest, identifier)
            assert row.next_delivery_at > datetime.now(UTC)
            assert row.delivery_attempts >= 1
        attempts.append(identifier)
        raise OSError("lost broker acknowledgement")

    _with_async_db(lambda db: publish_due(db, request_id=key, enqueue=lost_enqueue))
    _with_async_db(lambda db: publish_due(db, request_id=key, enqueue=lost_enqueue))
    assert attempts == [key]
    with sync_session() as db:
        db.get(SlidePostAIRequest, key).next_delivery_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
    _with_async_db(lambda db: publish_due(db, request_id=key, enqueue=lost_enqueue))
    assert attempts == [key, key]
    _with_async_db(lambda db: deliver(db, key))
    _with_async_db(lambda db: deliver(db, key))
    assert len(paid) == 1
    assert client.get(path(seeded) + "/" + str(key)).json()["status"] == "completed"


@pytest.mark.parametrize("change", ["draft", "source", "config", "project"])
def test_pending_changed_authority_or_executor_rejects_without_paid_call(
    client, seeded, paid, monkeypatch, change
):
    from sqlalchemy import select

    from app.models import PlanItemAsset
    from app.services.slide_post_ai_delivery import deliver

    _, key = _pending(client, seeded, monkeypatch)
    if change == "config":
        monkeypatch.setattr(settings, "custom_effects_enabled", not settings.custom_effects_enabled)
    else:
        with sync_session() as db:
            if change == "draft":
                item = db.get(PlanItem, seeded.item_id)
                item.slide_post = {**item.slide_post, "caption": "same-version replacement"}
            elif change == "source":
                asset = db.scalar(
                    select(PlanItemAsset).where(PlanItemAsset.plan_item_id == seeded.item_id)
                )
                asset.gcs_generation = "different-original-generation"
            else:
                db.get(CreationThread, seeded.thread_id).status = "archived"
            db.commit()
    _with_async_db(lambda db: deliver(db, key))
    with sync_session() as db:
        assert db.get(SlidePostAIRequest, key).status == "rejected"
    assert paid == []


def test_pending_cancel_is_per_original_intent_and_get_never_dispatches(
    client, seeded, paid, monkeypatch
):
    from app.services.slide_post_ai_delivery import deliver

    body, key = _pending(client, seeded, monkeypatch)
    for _ in range(3):
        assert client.get(path(seeded) + "/" + str(key)).json()["status"] == "pending"
    assert paid == []
    cancelled = client.post(path(seeded) + "/" + str(key) + "/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "rejected"
    _with_async_db(lambda db: deliver(db, key))
    assert client.post(path(seeded), json=body).json()["status"] == "rejected"
    assert paid == []


def test_same_uuid_reservation_uses_insert_winner_preparation(client, seeded, paid, monkeypatch):
    import copy

    from app.services.slide_post_ai_delivery import deliver

    body, key = _pending(client, seeded, monkeypatch)
    with sync_session() as db:
        original = copy.deepcopy(db.get(SlidePostAIRequest, key).prepared_input)
    loser = copy.deepcopy(original)
    loser["snapshot"]["agent_input"]["utterance"] = "loser-local preparation must not execute"

    async def reserve_loser(db):
        row = await SQLAIRequestStore(db).reserve(
            request_id=key,
            owner_id=seeded.user_id,
            item_id=seeded.item_id,
            project_id=seeded.thread_id,
            kind="chat_edit",
            body=body["input"],
            prepared_input=loser,
            authority_snapshot={"loser": True},
        )
        assert row.prepared_input == original
        await deliver(db, key)

    _with_async_db(reserve_loser)
    assert len(paid) == 1
    with sync_session() as db:
        assert db.get(SlidePostAIRequest, key).resolved_input == original


@pytest.mark.parametrize(
    "crash", ["before_enqueue", "before_claim", "after_claim", "after_provider", "after_completion"]
)
def test_process_death_never_reclaims_paid_execution(
    client, seeded, paid, monkeypatch, tmp_path, crash
):
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    from app.services.slide_post_ai_delivery import deliver

    if crash == "before_enqueue":
        body = payload(seeded)
        key = uuid.UUID(body["client_request_id"])
    else:
        body, key = _pending(client, seeded, monkeypatch)
    counter = tmp_path / "fake-provider-calls"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tests.slide_post_ai_kill_worker",
            json.dumps(
                [
                    str(key),
                    crash,
                    str(counter),
                    {"body": body, "item_id": str(seeded.item_id), "owner_id": str(seeded.user_id)},
                ]
            ),
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=dict(os.environ),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        import selectors

        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        assert selector.select(10), "guarded worker did not reach fault boundary"
        line = process.stdout.readline()
        assert line.strip() == "BOUNDARY", (
            line,
            process.stderr.read() if process.poll() is not None else "",
        )
        if crash in {"after_claim", "after_provider"}:
            duplicate = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "tests.slide_post_ai_kill_worker",
                    json.dumps([str(key), "observe", str(counter), None]),
                ],
                cwd=Path(__file__).resolve().parents[2],
                env=dict(os.environ),
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert duplicate.returncode == 0 and "OBSERVED" in duplicate.stdout, duplicate.stderr
        process.kill()
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.stdin.close()
        process.stdout.close()
        process.stderr.close()
    _with_async_db(lambda db: deliver(db, key))
    response = client.get(path(seeded) + "/" + str(key)).json()
    calls_in_child = counter.read_text().count("called") if counter.exists() else 0
    assert calls_in_child + len(paid) == (0 if crash == "after_claim" else 1)
    if crash in {"after_claim", "after_provider"}:
        assert response["status"] in {"running", "unknown"}
        assert client.post(path(seeded), json=body).json()["status"] in {"running", "unknown"}
    else:
        assert response["status"] == "completed"


def test_cancel_races_permanent_claim_without_reexecution(client, seeded, paid, monkeypatch):
    from app.services.slide_post_ai_delivery import deliver

    _, key = _pending(client, seeded, monkeypatch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        work = pool.submit(lambda: _with_async_db(lambda db: deliver(db, key)))
        stopped = pool.submit(lambda: client.post(path(seeded) + "/" + str(key) + "/cancel"))
        work.result(timeout=10)
        response = stopped.result(timeout=10)
    assert response.status_code in {200, 409}
    _with_async_db(lambda db: deliver(db, key))
    with sync_session() as db:
        row = db.get(SlidePostAIRequest, key)
        if row.status == "rejected":
            assert row.claim_id is None and paid == []
        else:
            assert row.status == "completed" and len(paid) == 1


def test_publication_batch_is_bounded_fair_and_has_no_attempt_cap(
    client, seeded, paid, monkeypatch
):
    import copy
    from datetime import UTC, datetime, timedelta

    from app.services.slide_post_ai_delivery import publish_due
    from app.services.slide_post_ai_requests import input_digest, unclaimed_pending

    _, original_key = _pending(client, seeded, monkeypatch)
    keys = [original_key]
    with sync_session() as db:
        original = db.get(SlidePostAIRequest, original_key)
        original.next_delivery_at = datetime.now(UTC) - timedelta(days=1)
        original.delivery_attempts = 100
        for _ in range(50):
            key = uuid.uuid4()
            keys.append(key)
            request_input = copy.deepcopy(original.request_input)
            request_input["client_request_id"] = str(key)
            prepared_input = copy.deepcopy(original.prepared_input)
            prepared_input["snapshot"]["context"]["request_id"] = f"slide-post-ai:{key}"
            db.add(
                SlidePostAIRequest(
                    id=key,
                    owner_id=original.owner_id,
                    item_id=original.item_id,
                    project_id=original.project_id,
                    kind=original.kind,
                    input_digest=input_digest(original.kind, request_input),
                    request_input=request_input,
                    prepared_input=prepared_input,
                    authority_snapshot=copy.deepcopy(original.authority_snapshot),
                    status="pending",
                    next_delivery_at=datetime.now(UTC) - timedelta(days=1),
                    delivery_attempts=100,
                )
            )
        db.commit()
    with sync_session() as observer:
        assert all(unclaimed_pending(observer.get(SlidePostAIRequest, key)) for key in keys)
    published = []

    def enqueue(key):
        published.append(key)
        if key == original_key:
            raise ValueError("one poison publication")

    from sqlalchemy import select as real_select

    monkeypatch.setattr(
        "app.services.slide_post_ai_delivery.select",
        lambda *entities: real_select(*entities).where(
            SlidePostAIRequest.owner_id == seeded.user_id
        ),
    )
    assert _with_async_db(lambda db: publish_due(db, enqueue=enqueue)) == 50
    assert len(published) == 50
    assert _with_async_db(lambda db: publish_due(db, enqueue=enqueue)) == 1
    assert set(published) == set(keys)
    assert paid == []


@pytest.mark.parametrize(
    "flag",
    [
        "slide_post_ai_recovery_enabled",
        "slide_posts_enabled",
        "slide_post_chat_edit_enabled",
        "slide_post_rich_text_enabled",
    ],
)
def test_pending_delivery_pauses_on_execution_rollback_and_resumes_original(
    client, seeded, paid, monkeypatch, flag
):
    from app.services.slide_post_ai_delivery import deliver

    _, key = _pending(client, seeded, monkeypatch)
    monkeypatch.setattr(settings, flag, False)
    _with_async_db(lambda db: deliver(db, key))
    with sync_session() as db:
        row = db.get(SlidePostAIRequest, key)
        assert row.status == "pending" and row.claim_id is None
    assert paid == []
    monkeypatch.setattr(settings, flag, True)
    _with_async_db(lambda db: deliver(db, key))
    assert len(paid) == 1


def test_concurrent_same_uuid_distinct_preparations_both_use_insert_winner(
    client, seeded, paid, monkeypatch
):
    import copy

    from app.services.slide_post_ai_delivery import deliver

    body, source_key = _pending(client, seeded, monkeypatch)
    with sync_session() as db:
        row = db.get(SlidePostAIRequest, source_key)
        original, authority = (
            copy.deepcopy(row.prepared_input),
            copy.deepcopy(row.authority_snapshot),
        )
    key = uuid.uuid4()
    body["input"]["client_request_id"] = str(key)
    candidates = []
    for name in ("first-preparation", "second-preparation"):
        candidate = copy.deepcopy(original)
        candidate["snapshot"]["context"]["request_id"] = f"slide-post-ai:{key}"
        candidate["snapshot"]["context"]["test_run_id"] = name
        candidates.append(candidate)

    async def reserve(db, prepared):
        row = await SQLAIRequestStore(db).reserve(
            request_id=key,
            owner_id=seeded.user_id,
            item_id=seeded.item_id,
            project_id=seeded.thread_id,
            kind="chat_edit",
            body=body["input"],
            prepared_input=prepared,
            authority_snapshot=authority,
        )
        return copy.deepcopy(row.prepared_input)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda prepared: _with_async_db(lambda db: reserve(db, prepared)), candidates)
        )
    assert results[0] == results[1] and results[0] in candidates
    _with_async_db(lambda db: deliver(db, key))
    _with_async_db(lambda db: deliver(db, key))
    with sync_session() as db:
        assert db.get(SlidePostAIRequest, key).resolved_input == results[0]
    assert len(paid) == 1


def test_locked_source_leaves_original_pending_then_resumes(client, seeded, paid, monkeypatch):
    from sqlalchemy import select

    from app.models import PlanItemAsset
    from app.services.slide_post_ai_delivery import deliver

    _, key = _pending(client, seeded, monkeypatch)
    with sync_session() as blocker:
        blocker.execute(
            select(PlanItemAsset)
            .where(PlanItemAsset.plan_item_id == seeded.item_id)
            .with_for_update()
        )
        _with_async_db(lambda db: deliver(db, key))
        assert paid == []
        blocker.rollback()
    with sync_session() as db:
        assert db.get(SlidePostAIRequest, key).status == "pending"
    _with_async_db(lambda db: deliver(db, key))
    assert len(paid) == 1
