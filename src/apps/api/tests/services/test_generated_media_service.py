"""Operation replay over real PostgreSQL and native admission; provider/storage are offline."""

import os
import uuid
from datetime import date
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import storage
from app.agents._runtime import RunContext
from app.config import settings
from app.database import sync_engine
from app.models import ContentPlan, Persona, PlanItem, User
from app.services.generated_media import (
    GeneratedMediaRequest,
    execute_generated_media,
    retry_generated_storage,
)
from app.services.generated_media_transport import OpenAIGeneratedTransport
from tests.services.test_generated_media_transport import image_response


@pytest.fixture
def generated(monkeypatch):
    if sync_engine.url.database != "nova_generated_test" and not (
        os.environ.get("CI") and sync_engine.url.database == "nova_test"
    ):
        pytest.skip("requires isolated nova_generated_test database")
    for key, value in {
        "ai_cost_control_enabled": True,
        "generated_images_enabled": True,
        "generated_narration_enabled": True,
        "generated_media_live_approved": True,
        "generated_media_billing_organization": "org-offline",
        "generated_media_price_verified_at": date.today().isoformat(),
        "generated_media_max_request_usd": 2.0,
        "ai_openai_monthly_budget_usd": 1000,
        "ai_openai_image_budget_usd": 1000,
        "ai_openai_narration_budget_usd": 1000,
        "ai_development_monthly_budget_usd": 1000,
    }.items():
        monkeypatch.setattr(settings, key, value)
    with Session(sync_engine, expire_on_commit=False) as db:
        user = User(email=f"generated-{uuid.uuid4()}@example.test")
        db.add(user)
        db.flush()
        persona = Persona(user_id=user.id)
        db.add(persona)
        db.flush()
        plan = ContentPlan(user_id=user.id, persona_id=persona.id)
        db.add(plan)
        db.flush()
        item = PlanItem(content_plan_id=plan.id, position=0, idea="offline")
        db.add(item)
        db.commit()
        client = Mock()
        client.with_options.return_value = client
        client.images.generate.return_value.model_dump.return_value = image_response()
        transport = OpenAIGeneratedTransport(client=client)
        objects = {}

        def upload(local, path, mime):
            objects[path] = Path(local).read_bytes()
            return storage.ObjectMetadata(path, "77", None, len(objects[path]), mime)

        def download(path, local, *, generation):
            assert generation == "77"
            Path(local).write_bytes(objects[path])

        monkeypatch.setattr(storage, "upload_local_file_immutable", upload)
        monkeypatch.setattr(storage, "download_generation_to_file", download)
        context = RunContext(
            creator_id=str(user.id),
            usage_purpose="live_eval",
            test_run_id="generated:" + str(uuid.uuid4()),
            estimated_max_cost_usd=10,
            reservation_approved=True,
        )
        yield db, user, item, client, transport, context
        db.rollback()
        db.execute(
            text("DELETE FROM ai_cost_reservations WHERE test_run_id=:run"),
            {"run": context.test_run_id},
        )
        db.execute(text("DELETE FROM content_plans WHERE id=:id"), {"id": plan.id})
        db.execute(text("DELETE FROM personas WHERE id=:id"), {"id": persona.id})
        db.execute(text("DELETE FROM users WHERE id=:id"), {"id": user.id})
        db.commit()


def test_generated_replay_has_one_paid_call(generated):
    db, owner, item, client, transport, ctx = generated
    request = GeneratedMediaRequest(
        client_event_id=uuid.uuid4(), capability="image", prompt="Watercolor"
    )
    first = execute_generated_media(
        db, owner_id=owner.id, item_id=item.id, body=request, ctx=ctx, transport=transport
    )
    assert first.state == "ready" and first.asset_id
    again = execute_generated_media(
        db, owner_id=owner.id, item_id=item.id, body=request, ctx=ctx, transport=transport
    )
    assert again.id == first.id
    assert client.images.generate.call_count == 1
    assert (
        db.execute(
            text("SELECT status FROM ai_cost_reservations WHERE id=:id"),
            {"id": first.reservation_id},
        ).scalar_one()
        == "settled"
    )
    with pytest.raises(ValueError, match="Idempotency"):
        execute_generated_media(
            db,
            owner_id=owner.id,
            item_id=item.id,
            body=request.model_copy(update={"prompt": "Changed"}),
            ctx=ctx,
            transport=transport,
        )


def test_unknown_never_regenerates(generated):
    db, owner, item, client, transport, ctx = generated
    client.images.generate.side_effect = TimeoutError("provider may have accepted")
    request = GeneratedMediaRequest(
        client_event_id=uuid.uuid4(), capability="image", prompt="Watercolor"
    )
    result = execute_generated_media(
        db, owner_id=owner.id, item_id=item.id, body=request, ctx=ctx, transport=transport
    )
    assert result.state == "unknown"
    again = execute_generated_media(
        db, owner_id=owner.id, item_id=item.id, body=request, ctx=ctx, transport=transport
    )
    assert again.state == "unknown" and client.images.generate.call_count == 1
    assert (
        db.execute(
            text("SELECT status FROM ai_cost_reservations WHERE id=:id"),
            {"id": result.reservation_id},
        ).scalar_one()
        == "unknown"
    )


@pytest.mark.parametrize(
    "failure",
    [
        OSError("storage offline"),
        __import__(
            "google.api_core.exceptions", fromlist=["ServiceUnavailable"]
        ).ServiceUnavailable("offline"),
    ],
)
def test_storage_retry_uses_saved_provider_bytes(generated, monkeypatch, failure):
    db, owner, item, client, transport, ctx = generated
    upload = storage.upload_local_file_immutable
    monkeypatch.setattr(storage, "upload_local_file_immutable", Mock(side_effect=failure))
    request = GeneratedMediaRequest(
        client_event_id=uuid.uuid4(), capability="image", prompt="Watercolor"
    )
    result = execute_generated_media(
        db, owner_id=owner.id, item_id=item.id, body=request, ctx=ctx, transport=transport
    )
    assert result.state == "provider_complete" and result.pending_content
    monkeypatch.setattr(storage, "upload_local_file_immutable", upload)
    result = retry_generated_storage(db, result.id, owner_id=owner.id, item_id=item.id)
    assert result.state == "ready" and result.pending_content is None
    assert client.images.generate.call_count == 1


def test_disabled_and_foreign_owner_never_submit(generated, monkeypatch):
    db, owner, item, client, transport, ctx = generated
    request = GeneratedMediaRequest(
        client_event_id=uuid.uuid4(), capability="image", prompt="Watercolor"
    )
    monkeypatch.setattr(settings, "generated_images_enabled", False)
    with pytest.raises(ValueError):
        execute_generated_media(
            db, owner_id=owner.id, item_id=item.id, body=request, ctx=ctx, transport=transport
        )
    monkeypatch.setattr(settings, "generated_images_enabled", True)
    with pytest.raises(ValueError):
        execute_generated_media(
            db, owner_id=uuid.uuid4(), item_id=item.id, body=request, ctx=ctx, transport=transport
        )
    assert client.images.generate.call_count == 0


def test_narration_requires_current_script_and_explicit_accept(generated, monkeypatch):
    import hashlib

    from sqlalchemy import event

    from app.models import CreationThread
    from app.services.generated_asset_contracts import NarrationRequest, decode_narration
    from app.services.generated_media import GeneratedMediaAccept, accept_generated_media
    from tests.agents.test_credit_plan_contracts import CreditPlanContracts

    db, owner, item, client, transport, ctx = generated
    item.voiceover_script = {"version": 1, "text": "Merhaba dünya.", "read_time_s": 1}
    db.add(
        CreationThread(
            creator_id=owner.id,
            content_plan_id=item.content_plan_id,
            active_plan_item_id=item.id,
        )
    )
    db.commit()
    script_hash = hashlib.sha256("Merhaba dünya.".encode()).hexdigest()
    body = GeneratedMediaRequest(
        client_event_id=uuid.uuid4(),
        capability="narration",
        prompt="Merhaba dünya.",
        script_id=f"item:{item.id}",
        script_version=1,
        approved_script_sha256=script_hash,
    )
    fixture = CreditPlanContracts().audio_events()
    fixture[-1]["response"]["usage"]["output_token_details"] = {
        "audio_tokens": 20,
        "text_tokens": 0,
    }
    asset = decode_narration(fixture, NarrationRequest("Merhaba dünya."))
    transport.narrate = Mock(return_value=asset)
    result = execute_generated_media(
        db, owner_id=owner.id, item_id=item.id, body=body, ctx=ctx, transport=transport
    )
    assert result.state == "ready"
    db.refresh(item)
    assert item.voiceover_gcs_path is None
    monkeypatch.setattr(
        storage,
        "object_metadata",
        lambda path: storage.ObjectMetadata(path, "77", None, len(asset.content), "audio/wav"),
    )
    accept = GeneratedMediaAccept(
        script_id=body.script_id, script_version=1, approved_script_sha256=script_hash
    )
    locked_tables = []

    def record_locks(connection, cursor, statement, parameters, context, executemany):
        sql = " ".join(statement.split())
        if "FOR UPDATE" in sql or "FOR NO KEY UPDATE" in sql:
            for table in ("personas", "content_plans", "plan_items", "creation_threads"):
                if f"FROM {table} " in sql:
                    locked_tables.append(table)

    event.listen(sync_engine, "before_cursor_execute", record_locks)
    try:
        accept_generated_media(db, result.id, owner_id=owner.id, item_id=item.id, body=accept)
    finally:
        event.remove(sync_engine, "before_cursor_execute", record_locks)
    assert list(dict.fromkeys(locked_tables)) == [
        "personas",
        "content_plans",
        "plan_items",
        "creation_threads",
    ]
    db.refresh(item)
    assert item.voiceover_gcs_path == result.output_path and item.audio_mode == "voiceover"
    item.voiceover_script = {"version": 2, "text": "New words", "read_time_s": 1}
    db.commit()
    with pytest.raises(ValueError, match="Script changed"):
        accept_generated_media(db, result.id, owner_id=owner.id, item_id=item.id, body=accept)
    assert transport.narrate.call_count == 1


def test_edit_pins_owned_source_generation(generated, monkeypatch):
    db, owner, item, client, transport, ctx = generated
    initial = execute_generated_media(
        db,
        owner_id=owner.id,
        item_id=item.id,
        body=GeneratedMediaRequest(
            client_event_id=uuid.uuid4(), capability="image", prompt="Original"
        ),
        ctx=ctx,
        transport=transport,
    )
    client.images.edit.return_value.model_dump.return_value = image_response()
    content = __import__("base64").b64decode(image_response()["data"][0]["b64_json"])
    monkeypatch.setattr(
        storage,
        "object_metadata",
        lambda path: storage.ObjectMetadata(path, "77", None, len(content), "image/png"),
    )
    body = GeneratedMediaRequest(
        client_event_id=uuid.uuid4(),
        capability="image_edit",
        prompt="Change the color",
        source_asset_id=initial.asset_id,
        source_generation="78",
    )
    with pytest.raises(ValueError, match="source changed"):
        execute_generated_media(
            db, owner_id=owner.id, item_id=item.id, body=body, ctx=ctx, transport=transport
        )
    body.source_generation = "77"
    result = execute_generated_media(
        db, owner_id=owner.id, item_id=item.id, body=body, ctx=ctx, transport=transport
    )
    assert result.state == "ready" and result.asset_id != initial.asset_id
    assert client.images.edit.call_count == 1


def test_stale_submission_is_closed_without_releasing_liability(generated):
    from datetime import UTC, datetime, timedelta

    from app.services.generated_media import reconcile_generated_media

    db, owner, item, client, transport, ctx = generated
    client.images.generate.side_effect = TimeoutError()
    body = GeneratedMediaRequest(
        client_event_id=uuid.uuid4(), capability="image", prompt="Background"
    )
    operation = execute_generated_media(
        db, owner_id=owner.id, item_id=item.id, body=body, ctx=ctx, transport=transport
    )
    operation.state = "submitting"
    operation.updated_at = datetime.now(UTC) - timedelta(minutes=10)
    db.commit()
    assert reconcile_generated_media(db)["stale"] == 1
    db.refresh(operation)
    assert operation.state == "unknown"
    assert (
        db.execute(
            text("SELECT status FROM ai_cost_reservations WHERE id=:id"),
            {"id": operation.reservation_id},
        ).scalar_one()
        == "unknown"
    )


def test_native_http_submit_preview_accept_and_owner_fence(generated, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.auth import get_current_user, is_native_client, kria_client_protocol
    from app.routes.generated_media import router
    from app.services import generated_media as service

    db, owner, item, client, transport, ctx = generated
    monkeypatch.setattr(settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(settings, "phone_render_verified_features", ["visualBlocks"])
    monkeypatch.setattr(settings, "ai_manual_qa_test_run_id", ctx.test_run_id)
    monkeypatch.setattr(settings, "ai_manual_qa_reservation_approved", True)
    monkeypatch.setattr(settings, "ai_manual_qa_max_cost_usd", 2.0)
    monkeypatch.setattr(service, "OpenAIGeneratedTransport", lambda: transport)
    monkeypatch.setattr(
        storage,
        "signed_get_url_for_generation",
        lambda path, generation: "https://owned.test/preview.png",
    )
    app = FastAPI()
    app.include_router(router, prefix="/plan-items")
    app.dependency_overrides[get_current_user] = lambda: owner
    app.dependency_overrides[is_native_client] = lambda: True
    app.dependency_overrides[kria_client_protocol] = lambda: 99
    http = TestClient(app)
    body = {"client_event_id": str(uuid.uuid4()), "capability": "image", "prompt": "Background"}
    response = http.post(f"/plan-items/{item.id}/generated-media", json=body)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["state"] == "ready" and result["preview_url"] and result["asset_id"]
    assert (
        http.get(
            f"/plan-items/{item.id}/generated-media/by-event/{body['client_event_id']}"
        ).json()["id"]
        == result["id"]
    )
    assert (
        http.post(
            f"/plan-items/{item.id}/generated-media/{result['id']}/accept", json={}
        ).status_code
        == 200
    )
    assert client.images.generate.call_count == 1
    foreign = User(id=uuid.uuid4(), email="foreign@example.test")
    app.dependency_overrides[get_current_user] = lambda: foreign
    assert http.get(f"/plan-items/{item.id}/generated-media/{result['id']}").status_code == 404
    assert client.images.generate.call_count == 1


def test_concurrent_duplicate_submissions_contact_provider_once(generated):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    db, owner, item, client, transport, ctx = generated
    entered, finish = Event(), Event()
    sdk_response = client.images.generate.return_value

    def held_provider(**kwargs):
        entered.set()
        assert finish.wait(5)
        return sdk_response

    client.images.generate.side_effect = held_provider
    owner_id, item_id = owner.id, item.id
    body = GeneratedMediaRequest(
        client_event_id=uuid.uuid4(), capability="image", prompt="Background"
    )

    def submit():
        with Session(sync_engine, expire_on_commit=False) as session:
            result = execute_generated_media(
                session, owner_id=owner_id, item_id=item_id, body=body, ctx=ctx, transport=transport
            )
            return result.id, result.state

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(submit)
        try:
            assert entered.wait(5)
            second = pool.submit(submit).result(timeout=5)
            assert second[1] == "submitting"
        finally:
            finish.set()
        done = first.result(timeout=5)
    assert done == (second[0], "ready")
    assert client.images.generate.call_count == 1


def test_edit_reuses_only_verified_owned_library_original(generated, monkeypatch):
    import base64
    import hashlib

    from app.models import MediaAsset, MediaAssetSource, PlanItemAsset

    db, owner, item, client, transport, ctx = generated
    content = base64.b64decode(image_response()["data"][0]["b64_json"])
    media = MediaAsset(
        user_id=owner.id, kind="image", status="ready", original_availability="cloud_backed"
    )
    db.add(media)
    db.flush()
    path = f"users/{owner.id}/library/{media.id}/original.png"
    source = MediaAssetSource(
        user_id=owner.id,
        asset_id=media.id,
        role="original",
        object_path=path,
        object_generation="7",
        uploaded_sha256=hashlib.sha256(content).hexdigest(),
        byte_count=len(content),
        content_type="image/png",
        status="available",
    )
    db.add(source)
    db.flush()
    pool = PlanItemAsset(
        user_id=owner.id,
        plan_item_id=item.id,
        kind="image",
        status="ready",
        media_status="ready",
        library_asset_id=media.id,
        library_source_id=source.id,
        gcs_path=path,
        gcs_generation="7",
    )
    db.add(pool)
    db.commit()
    download = storage.download_generation_to_file

    def source_download(path_value, local, *, generation):
        if path_value == path:
            assert generation == "7"
            Path(local).write_bytes(content)
        else:
            download(path_value, local, generation=generation)

    monkeypatch.setattr(storage, "download_generation_to_file", source_download)
    monkeypatch.setattr(
        storage,
        "object_metadata",
        lambda value: storage.ObjectMetadata(value, "7", None, len(content), "image/png"),
    )
    client.images.edit.return_value.model_dump.return_value = image_response()
    body = GeneratedMediaRequest(
        client_event_id=uuid.uuid4(),
        capability="image_edit",
        prompt="Blue background",
        source_asset_id=pool.id,
        source_generation="7",
    )
    result = execute_generated_media(
        db, owner_id=owner.id, item_id=item.id, body=body, ctx=ctx, transport=transport
    )
    assert result.state == "ready"
    content = b"x" + content[1:]
    with pytest.raises(ValueError, match="verified identity"):
        execute_generated_media(
            db,
            owner_id=owner.id,
            item_id=item.id,
            body=body.model_copy(update={"client_event_id": uuid.uuid4()}),
            ctx=ctx,
            transport=transport,
        )
    assert client.images.edit.call_count == 1


def test_legacy_opaque_request_replays_after_transparency_field_is_added(generated):
    import hashlib
    import json

    db, owner, item, client, transport, ctx = generated
    request = GeneratedMediaRequest(
        client_event_id=uuid.uuid4(), capability="image", prompt="Watercolor"
    )
    first = execute_generated_media(
        db, owner_id=owner.id, item_id=item.id, body=request, ctx=ctx, transport=transport
    )
    legacy = request.model_dump(mode="json")
    legacy.pop("transparent_background")
    first.request_digest = hashlib.sha256(json.dumps(legacy, sort_keys=True).encode()).hexdigest()
    db.commit()
    again = execute_generated_media(
        db, owner_id=owner.id, item_id=item.id, body=request, ctx=ctx, transport=transport
    )
    assert again.id == first.id
    assert client.images.generate.call_count == 1
    with pytest.raises(ValueError, match="Idempotency key reused"):
        execute_generated_media(
            db,
            owner_id=owner.id,
            item_id=item.id,
            body=request.model_copy(update={"transparent_background": True}),
            ctx=ctx,
            transport=transport,
        )
    assert client.images.generate.call_count == 1
