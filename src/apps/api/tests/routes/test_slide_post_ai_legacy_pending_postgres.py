# ruff: noqa: F811
"""Legacy pending is uncertainty, not evidence that no paid work occurred.

Failure modes: missing envelope falsely rejected; old token overwritten; cancellation
falsely definitive; legacy rows starve current delivery; changed input/source/tenant
reconstructs or exposes a private old result. All providers and broker calls are fakes.
"""

import copy
import json
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import null, select

from app.database import sync_session
from app.models import CreationThread, PlanItem, PlanItemAsset, SlidePostAIRequest
from app.services.slide_post_ai_requests import SQLAIRequestStore, input_digest
from tests.routes.test_slide_post_ai_requests_postgres import (  # noqa: F401
    _flags,
    _pending,
    _with_async_db,
    client,
    paid,
    path,
    payload,
    seeded,
)


def legacy(seeded, *, token=None, prepared=None, authority=None):
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
                claim_id=token,
                claimed_at=datetime.now(UTC) if token else None,
                prepared_input=prepared,
                authority_snapshot=authority,
            )
        )
        db.commit()
    return body, key


@pytest.mark.parametrize("has_token", [False, True])
def test_legacy_pending_process_retry_and_cancel_remain_unknown(
    client, seeded, paid, tmp_path, has_token
):
    token = uuid.uuid4() if has_token else None
    body, key = legacy(seeded, token=token)
    marker = tmp_path / "provider-count"
    # A fresh worker may encounter an old row after process death. It must not
    # infer nonexecution from fields the older writer never persisted.
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.slide_post_ai_kill_worker",
            json.dumps([str(key), "observe", str(marker), None]),
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert process.returncode == 0 and "OBSERVED" in process.stdout, process.stderr
    assert not marker.exists()
    observed = client.get(path(seeded) + "/" + str(key))
    assert observed.json()["status"] == "unknown" and observed.json()["delivery_version"] == 0
    assert observed.json()["result"] is None
    assert client.post(path(seeded), json=body).json()["status"] == "unknown"
    cancelled = client.post(path(seeded) + "/" + str(key) + "/cancel")
    assert cancelled.status_code == 409
    assert client.get(path(seeded) + "/" + str(key)).json()["status"] == "unknown"
    with sync_session() as db:
        row = db.get(SlidePostAIRequest, key)
        assert row.status == "pending" and row.claim_id == token and row.result is None
        assert row.delivery_attempts == 0 and row.next_delivery_at is None
    assert paid == []


def test_pending_exact_old_token_may_publish_once_but_null_wrong_cancelled_cannot(
    client, seeded, paid
):
    token = uuid.uuid4()
    _, key = legacy(seeded, token=token)
    _, no_token = legacy(seeded)
    result = {"outcome": "no_effect", "reply": "verified original result", "base_version": 1}

    async def completion(db):
        store = SQLAIRequestStore(db)
        assert not await store.complete(key, None, {"bad": True})
        assert not await store.complete(key, uuid.uuid4(), {"bad": True})
        assert not await store.complete(no_token, None, {"bad": True})

    _with_async_db(completion)
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.slide_post_ai_kill_worker",
            json.dumps(
                [str(key), "legacy_complete", "unused", {"claim_id": str(token), "result": result}]
            ),
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert process.returncode == 0 and "COMPLETED" in process.stdout, process.stderr
    assert not _with_async_db(
        lambda db: SQLAIRequestStore(db).complete(key, token, {"replacement": True})
    )
    _, cancelled = legacy(seeded, token=token)
    with sync_session() as db:
        db.get(SlidePostAIRequest, cancelled).status = "rejected"
        db.commit()
    assert not _with_async_db(lambda db: SQLAIRequestStore(db).complete(cancelled, token, result))
    assert client.get(path(seeded) + "/" + str(key)).json()["result"] == result
    assert paid == []


@pytest.mark.parametrize(
    "missing",
    [
        "prepared",
        "authority",
        "both",
        "token",
        "timestamp",
        "resolved",
        "bool_version",
        "float_version",
        "result_without_digest",
        "partial_manifest",
    ],
)
def test_ambiguous_pending_never_claims_or_cancels_even_with_partial_envelope(
    client, seeded, paid, monkeypatch, missing
):
    from app.services.slide_post_ai_delivery import deliver

    _, key = _pending(client, seeded, monkeypatch)
    token = uuid.uuid4() if missing == "token" else None
    with sync_session() as db:
        row = db.get(SlidePostAIRequest, key)
        snapshot = copy.deepcopy(row.prepared_input)
        if missing in {"prepared", "both"}:
            row.prepared_input = None
        if missing in {"authority", "both"}:
            row.authority_snapshot = {}
        if missing == "bool_version":
            row.prepared_input = {**row.prepared_input, "version": True}
        if missing == "float_version":
            row.prepared_input = {**row.prepared_input, "version": 1.0}
        if missing == "result_without_digest":
            row.prepared_input = {"version": 1, "result": {"reply": "old"}}
        if missing == "partial_manifest":
            row.prepared_input = {**row.prepared_input, "manifest": {"unknown_old_config": True}}
        if missing in {"bool_version", "float_version"}:
            # ORM JSON equality treats 1 == True == 1.0; force the deliberately
            # malformed wire representation to reach PostgreSQL.
            from sqlalchemy.orm.attributes import flag_modified

            flag_modified(row, "prepared_input")
        row.claim_id = token
        if missing == "timestamp":
            row.claimed_at = datetime.now(UTC)
        if missing == "resolved":
            row.resolved_input = {"old_paid_input": True}
        db.commit()

    if missing in {"bool_version", "float_version"}:
        with sync_session() as observer:
            stored = observer.get(SlidePostAIRequest, key).prepared_input["version"]
            assert type(stored) is (bool if missing == "bool_version" else float)

    async def try_claim(db):
        assert await SQLAIRequestStore(db).claim(key, snapshot) is None
        assert not await SQLAIRequestStore(db).cancel(key)
        await deliver(db, key)

    _with_async_db(try_claim)
    assert client.get(path(seeded) + "/" + str(key)).json()["status"] == "unknown"
    with sync_session() as db:
        assert db.get(SlidePostAIRequest, key).claim_id == token
    assert paid == []


def test_more_than_fifty_incomplete_legacy_rows_do_not_starve_current_delivery(
    client, seeded, paid, monkeypatch
):
    from app.services import slide_post_ai_delivery as delivery

    _, current = _pending(client, seeded, monkeypatch)
    old_keys = []
    with sync_session() as db:
        template = db.get(SlidePostAIRequest, current)
        for index in range(55):
            key = uuid.uuid4()
            old_keys.append(key)
            db.add(
                SlidePostAIRequest(
                    id=key,
                    owner_id=template.owner_id,
                    item_id=template.item_id,
                    project_id=template.project_id,
                    kind=template.kind,
                    request_input=copy.deepcopy(template.request_input),
                    input_digest=template.input_digest,
                    status="pending",
                    created_at=datetime.now(UTC) - timedelta(days=1),
                    prepared_input=(null() if index % 3 == 0 else None if index % 3 == 1 else {})
                    if index % 6 < 3
                    else copy.deepcopy(template.prepared_input),
                    authority_snapshot=copy.deepcopy(template.authority_snapshot)
                    if index % 6 < 3
                    else (null() if index % 3 == 0 else None if index % 3 == 1 else {}),
                )
            )
        db.commit()
    monkeypatch.setattr(
        delivery,
        "select",
        lambda *entities: select(*entities).where(SlidePostAIRequest.owner_id == seeded.user_id),
    )
    published = []
    assert _with_async_db(lambda db: delivery.publish_due(db, enqueue=published.append)) == 1
    assert published == [current]
    _with_async_db(lambda db: delivery.deliver(db, current))
    assert len(paid) == 1
    with sync_session() as db:
        assert all(db.get(SlidePostAIRequest, key).delivery_attempts == 0 for key in old_keys)
    assert len(paid) == 1


def test_legacy_unknown_new_instruction_source_and_project_do_not_reconstruct(client, seeded, paid):
    body, key = legacy(seeded)
    changed = {**body, "input": {**body["input"], "message": "new instruction"}}
    assert client.post(path(seeded), json=changed).status_code == 409
    with sync_session() as db:
        item = db.get(PlanItem, seeded.item_id)
        asset = db.scalar(select(PlanItemAsset).where(PlanItemAsset.plan_item_id == seeded.item_id))
        asset.gcs_generation = "source-replacement-after-original-request"
        item.slide_post = {**item.slide_post, "caption": "changed source context", "version": 2}
        db.commit()
    assert client.post(path(seeded), json=body).json()["status"] == "unknown"
    with sync_session() as db:
        db.get(CreationThread, seeded.thread_id).active_plan_item_id = seeded.other_item_id
        db.commit()
    assert client.get(path(seeded) + "/" + str(key)).status_code == 404
    assert client.post(path(seeded), json=body).status_code == 409
    assert paid == []


def test_legacy_unknown_other_owner_cannot_read_cancel_or_submit(client, seeded, paid):
    from types import SimpleNamespace

    from app.auth import get_current_user
    from app.main import app
    from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

    body, key = legacy(seeded, token=uuid.uuid4())
    _, other_thread_id, _ = _seed_runtime_project()
    with sync_session() as db:
        other_owner = db.get(CreationThread, other_thread_id).creator_id
    original = app.dependency_overrides[get_current_user]
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=other_owner)
    try:
        assert client.get(path(seeded) + "/" + str(key)).status_code == 404
        assert client.post(path(seeded) + "/" + str(key) + "/cancel").status_code == 404
        assert client.post(path(seeded), json=body).status_code == 404
    finally:
        app.dependency_overrides[get_current_user] = original
    assert client.get(path(seeded) + "/" + str(key)).json()["status"] == "unknown"
    assert paid == []


def test_started_fake_provider_then_legacy_fault_never_replays_and_original_token_completes(
    client, seeded, paid, monkeypatch, tmp_path
):
    """Fault-injected legacy representation, not execution of a historical binary."""
    import selectors

    _, key = _pending(client, seeded, monkeypatch)
    counter = tmp_path / "actual-fake-call"
    command = [sys.executable, "-m", "tests.slide_post_ai_kill_worker"]
    options = dict(cwd=Path(__file__).resolve().parents[2], env=dict(os.environ), text=True)
    process = subprocess.Popen(
        [*command, json.dumps([str(key), "after_provider", str(counter), None])],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **options,
    )
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(15), "fake provider did not reach the signal boundary"
        assert process.stdout.readline().strip() == "BOUNDARY"
        assert counter.read_text() == "called\n"
        process.kill()
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.stdin.close()
        process.stdout.close()
        process.stderr.close()
    with sync_session() as db:
        row = db.get(SlidePostAIRequest, key)
        token = row.claim_id
        assert token is not None and row.status == "running"
        row.status = "pending"
        row.prepared_input = None
        row.authority_snapshot = None
        db.commit()
    observe = subprocess.run(
        [*command, json.dumps([str(key), "observe", str(counter), None])],
        capture_output=True,
        timeout=15,
        **options,
    )
    assert observe.returncode == 0 and "OBSERVED" in observe.stdout, observe.stderr
    assert client.get(path(seeded) + "/" + str(key)).json()["status"] == "unknown"
    assert client.post(path(seeded) + "/" + str(key) + "/cancel").status_code == 409
    result = {"outcome": "no_effect", "reply": "verified original fake result", "base_version": 1}
    late = subprocess.run(
        [
            *command,
            json.dumps(
                [
                    str(key),
                    "legacy_complete",
                    str(counter),
                    {"claim_id": str(token), "result": result},
                ]
            ),
        ],
        capture_output=True,
        timeout=15,
        **options,
    )
    assert late.returncode == 0 and "COMPLETED" in late.stdout, late.stderr
    assert counter.read_text() == "called\n" and paid == []
    response = client.get(path(seeded) + "/" + str(key)).json()
    assert response["status"] == "completed" and response["result"] == result


@pytest.mark.parametrize(
    "kind,missing",
    [
        ("chat_edit", "baseline"),
        ("chat_edit", "context"),
        ("chat_edit", "server_version"),
        ("chat_edit", "binding"),
        ("chat_edit", "invalid_baseline"),
        ("propose", "fallback"),
        ("propose", "previous_version"),
        ("chat_edit", "authority_owner"),
    ],
)
def test_partial_effective_input_with_valid_manifest_never_pays_or_enters_sweep(
    client, seeded, paid, monkeypatch, kind, missing
):
    from app.services.slide_post_ai_delivery import deliver, publish_due

    if kind == "chat_edit":
        _, key = _pending(client, seeded, monkeypatch)
    else:

        async def pause(*args, **kwargs):
            pass

        body = payload(seeded)
        body["kind"] = "propose"
        body["input"] = {
            "expected_version": 1,
            "platform_profile": "tiktok_photo",
            "instruction": "Order these photos",
        }
        with monkeypatch.context() as patch:
            patch.setattr("app.services.slide_post_ai_delivery.deliver", pause)
            patch.setattr("app.services.slide_post_ai_delivery.publish_due", pause)
            reply = client.post(path(seeded), json=body)
        assert reply.status_code == 200 and reply.json()["status"] == "pending", reply.text
        key = uuid.UUID(body["client_request_id"])
        from app.agents.slide_post_composer import SlidePostComposerOutput

        class FakeComposer:
            def __init__(self, _):
                pass

            def run(self, value, ctx=None):
                paid.append(ctx.request_id)
                ids = [m.id for m in value.media]
                return SlidePostComposerOutput(order=ids, cover_id=ids[0], caption="fake")

        monkeypatch.setattr("app.services.slide_post_compose.SlidePostComposerAgent", FakeComposer)
    with sync_session() as db:
        row = db.get(SlidePostAIRequest, key)
        prepared = copy.deepcopy(row.prepared_input)
        if missing == "authority_owner":
            authority = copy.deepcopy(row.authority_snapshot)
            authority.pop("owner_id")
            row.authority_snapshot = authority
        elif missing == "invalid_baseline":
            prepared["snapshot"]["baseline"] = {"not_a_draft": True}
        else:
            prepared["snapshot"].pop(missing)
        row.prepared_input = prepared
        db.commit()
    published = []
    _with_async_db(lambda db: publish_due(db, request_id=key, enqueue=published.append))
    _with_async_db(lambda db: deliver(db, key))
    assert paid == []
    assert published == []
    with sync_session() as db:
        row = db.get(SlidePostAIRequest, key)
        assert row.status == "pending" and row.claim_id is None and row.delivery_attempts == 0
    assert client.get(path(seeded) + "/" + str(key)).json()["status"] == "unknown"


def test_semantically_invalid_baseline_cannot_be_definitively_cancelled_or_rejected(
    client, seeded, paid, monkeypatch
):
    _, key = _pending(client, seeded, monkeypatch)
    with sync_session() as db:
        row = db.get(SlidePostAIRequest, key)
        prepared = copy.deepcopy(row.prepared_input)
        prepared["snapshot"]["baseline"]["slides"] = ["bad"]
        row.prepared_input = prepared
        db.commit()
    assert client.get(path(seeded) + "/" + str(key)).json()["status"] == "unknown"
    assert client.post(path(seeded) + "/" + str(key) + "/cancel").status_code == 409
    _with_async_db(lambda db: SQLAIRequestStore(db).reject(key, "must not become definitive"))
    with sync_session() as db:
        assert db.get(SlidePostAIRequest, key).status == "pending"
    assert paid == []


@pytest.mark.parametrize("kind", ["chat_edit", "propose"])
def test_intact_captured_original_delivers_once_for_each_kind(
    client, seeded, paid, monkeypatch, kind
):
    from app.services.slide_post_ai_delivery import deliver, publish_due

    if kind == "chat_edit":
        _, key = _pending(client, seeded, monkeypatch)
    else:

        async def pause(*args, **kwargs):
            pass

        body = payload(seeded)
        body["kind"] = "propose"
        body["input"] = {
            "expected_version": 1,
            "platform_profile": "tiktok_photo",
            "instruction": "Order these photos",
        }
        with monkeypatch.context() as patch:
            patch.setattr("app.services.slide_post_ai_delivery.deliver", pause)
            patch.setattr("app.services.slide_post_ai_delivery.publish_due", pause)
            reply = client.post(path(seeded), json=body)
        assert reply.status_code == 200 and reply.json()["status"] == "pending", reply.text
        key = uuid.UUID(body["client_request_id"])
        from app.agents.slide_post_composer import SlidePostComposerOutput

        class FakeComposer:
            def __init__(self, _):
                pass

            def run(self, value, ctx=None):
                paid.append(ctx.request_id)
                ids = [m.id for m in value.media]
                return SlidePostComposerOutput(order=ids, cover_id=ids[0], caption="fake proposal")

        monkeypatch.setattr("app.services.slide_post_compose.SlidePostComposerAgent", FakeComposer)
    with sync_session() as db:
        original = copy.deepcopy(db.get(SlidePostAIRequest, key).prepared_input)
    published = []
    _with_async_db(lambda db: publish_due(db, request_id=key, enqueue=published.append))
    assert published == [key]
    _with_async_db(lambda db: deliver(db, key))
    _with_async_db(lambda db: deliver(db, key))
    assert len(paid) == 1
    with sync_session() as db:
        row = db.get(SlidePostAIRequest, key)
        assert row.status == "completed" and row.claim_id is not None
        assert row.resolved_input == original and row.result["base_version"] == 1
    assert client.get(path(seeded) + "/" + str(key)).json()["status"] == "completed"
