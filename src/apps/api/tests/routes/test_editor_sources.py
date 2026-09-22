import copy
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException, Response
from sqlalchemy.dialects import postgresql

from app.models import Job
from app.routes import editor_sources as routes
from app.services.phone_editor_sources import EDITOR_SOURCES_FIELD
from app.tasks import editor_sources as task
from tests.services.test_phone_editor_sources import admitted, source


@pytest.fixture
def admission(monkeypatch):
    user = SimpleNamespace(id=uuid.uuid4())
    item = SimpleNamespace(id=uuid.uuid4(), current_job_id=uuid.uuid4())
    asset_id = uuid.uuid4()
    variant = {"variant_id": "v", "render_generation_id": "first"}
    job = SimpleNamespace(
        id=item.current_job_id,
        user_id=user.id,
        content_plan_item_id=item.id,
        assembly_plan={"variants": [variant], "guided_edit": {"immutable": True}},
    )
    asset = SimpleNamespace(
        id=asset_id,
        plan_item_id=item.id,
        user_id=user.id,
        status="ready",
        kind="image",
        gcs_path="users/u/photo.jpg",
        gcs_generation="42",
    )
    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: job)),
        get=AsyncMock(side_effect=lambda model, *a, **kw: job if model is Job else asset),
        commit=AsyncMock(),
        refresh=AsyncMock(),
    )
    monkeypatch.setattr(routes, "_load_owned_item", AsyncMock(return_value=item))
    monkeypatch.setattr(routes, "flag_modified", Mock())
    monkeypatch.setattr(routes.settings, "phone_editor_media_enabled", True)
    monkeypatch.setattr(routes, "_phone_editor_media_available", lambda *args: True)
    # No persisted revision: the real shared helper derives revision one.
    monkeypatch.setattr(
        routes, "_guided_v2_revision", lambda *args: {"revision_number": 1, "sources": [source()]}
    )
    publish = Mock()
    monkeypatch.setattr(routes.prepare_phone_editor_source, "delay", publish)
    body = routes.EditorSourceRequest(
        client_import_id=uuid.uuid4(),
        base_generation="first",
        guided_revision_number=1,
        source_kind="visual",
        source_id=str(asset_id),
    )
    return SimpleNamespace(
        user=user,
        item=item,
        job=job,
        variant=variant,
        asset=asset,
        db=db,
        publish=publish,
        body=body,
    )


async def post(a, body=None):
    response = Response()
    out = await routes.create_editor_source(
        str(a.item.id), "v", body or a.body, a.user, response, a.db
    )
    return out, response


@pytest.mark.asyncio
async def test_initial_projection_accepts_import_without_persisting_revision(admission):
    a = admission
    out, response = await post(a)
    assert out.status == "preparing" and response.status_code == 202
    assert "guided_edit_revision" not in a.variant
    assert a.job.assembly_plan["guided_edit"] == {"immutable": True}
    token = a.variant[EDITOR_SOURCES_FIELD]["imports"][str(a.body.client_import_id)]["attempt_id"]
    a.publish.assert_called_once_with(str(a.job.id), "v", str(a.body.client_import_id), token)
    # Idempotent duplicate doesn't publish another worker.
    await post(a)
    a.publish.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"base_generation": "old"}, {"guided_revision_number": 2}])
async def test_stale_import_does_not_mutate_or_publish(admission, change):
    a = admission
    with pytest.raises(HTTPException) as exc:
        await post(a, a.body.model_copy(update=change))
    assert exc.value.status_code == 409
    assert EDITOR_SOURCES_FIELD not in a.variant
    a.db.commit.assert_not_called()
    a.publish.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed",
    ["capability", "job_owner", "job_item", "visual_owner", "visual_item", "visual_status"],
)
async def test_unauthorized_context_fails_before_mutation(admission, monkeypatch, changed):
    a = admission
    if changed == "capability":
        monkeypatch.setattr(routes, "_phone_editor_media_available", lambda *args: False)
    elif changed == "job_owner":
        a.job.user_id = uuid.uuid4()
    elif changed == "job_item":
        a.job.content_plan_item_id = uuid.uuid4()
    elif changed == "visual_owner":
        a.asset.user_id = uuid.uuid4()
    elif changed == "visual_item":
        a.asset.plan_item_id = uuid.uuid4()
    else:
        a.asset.status = "removed"
    with pytest.raises(HTTPException):
        await post(a)
    assert EDITOR_SOURCES_FIELD not in a.variant
    a.db.commit.assert_not_called()
    a.publish.assert_not_called()


@pytest.mark.asyncio
async def test_feature_off_does_not_load_item(admission, monkeypatch):
    monkeypatch.setattr(routes.settings, "phone_editor_media_enabled", False)
    with pytest.raises(HTTPException) as exc:
        await post(admission)
    assert exc.value.status_code == 404
    routes._load_owned_item.assert_not_called()


@pytest.mark.asyncio
async def test_import_id_cannot_be_reused_with_changed_payload(admission):
    await post(admission)
    with pytest.raises(HTTPException) as exc:
        await post(admission, admission.body.model_copy(update={"source_id": str(uuid.uuid4())}))
    assert exc.value.detail == "import_id_reused"
    admission.publish.assert_called_once()


@pytest.mark.asyncio
async def test_broker_failure_returns_retryable_failure_and_next_post_retries(
    admission, monkeypatch
):
    a = admission
    a.publish.side_effect = RuntimeError("secret broker password")

    def fail(job_id, variant_id, key, token, *, code, retryable):
        record = a.variant[EDITOR_SOURCES_FIELD]["imports"][key]
        assert record["attempt_id"] == token
        record.update(status="failed", error=code, reason_code=code, retryable=retryable)

    monkeypatch.setattr(task, "_fail", fail)
    out, response = await post(a)
    first = a.variant[EDITOR_SOURCES_FIELD]["imports"][str(a.body.client_import_id)]["attempt_id"]
    assert out.status == "failed" and out.retryable and response.status_code == 200
    assert out.error == "source_dispatch_failed"
    a.publish.side_effect = None
    out, response = await post(a)
    assert out.status == "preparing" and response.status_code == 202
    assert a.publish.call_count == 2
    assert a.publish.call_args.args[-1] != first


@pytest.mark.asyncio
async def test_expired_prepare_get_is_read_only_and_post_rotates_attempt(admission):
    a = admission
    await post(a)
    record = a.variant[EDITOR_SOURCES_FIELD]["imports"][str(a.body.client_import_id)]
    first = record["attempt_id"]
    record["lease_expires_at"] = 0
    before = copy.deepcopy(a.job.assembly_plan)
    out = await routes.get_editor_source(str(a.item.id), "v", a.body.client_import_id, a.user, a.db)
    assert out.status == "failed" and out.retryable and out.reason_code == "source_prepare_expired"
    assert a.job.assembly_plan == before
    await post(a)
    assert a.publish.call_count == 2
    assert a.publish.call_args.args[-1] != first


@pytest.mark.asyncio
async def test_ready_duplicate_keeps_index_and_typed_source(admission):
    a = admission
    await post(a)
    record = a.variant[EDITOR_SOURCES_FIELD]["imports"][str(a.body.client_import_id)]
    record.update(status="ready", source=source(), source_index=0)
    out, response = await post(a)
    assert response.status_code == 200 and out.source.media_id == "approved"
    assert out.source_index == 0
    assert "attempt_id" not in out.model_dump()
    assert "user_id" not in out.model_dump()
    a.publish.assert_called_once()


@pytest.mark.asyncio
async def test_reservation_query_is_scoped_to_item_project(admission):
    a = admission
    a.db.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: None)
    with pytest.raises(HTTPException, match="source_reservation_missing"):
        await routes._owned_proxy_reservation(
            a.db, user_id=a.user.id, source_id="clip", item_id=a.item.id
        )
    query = str(a.db.execute.call_args.args[0].compile(dialect=postgresql.dialect()))
    assert "creation_threads.active_plan_item_id =" in query
    assert "creation_threads.creator_id =" in query
    assert "FOR UPDATE OF creation_thread_upload_reservations" in query


@pytest.mark.asyncio
async def test_save_ignores_unused_admitted_sources(admission):
    a = admission
    a.variant[EDITOR_SOURCES_FIELD] = {"sources": [admitted("unused", 1)]}
    await routes.validate_editor_sources(a.db, job=a.job, variant=a.variant, used_media_ids=set())
    a.db.get.assert_not_called()
    a.db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_save_footage_uses_durable_receipt_after_reservation_consumed(admission, monkeypatch):
    a = admission
    row = admitted("clip", 1)
    row["source_binding"] = {"media_id": "clip", "proxy_path": row["gcs_path"], "generation": "42"}
    a.variant[EDITOR_SOURCES_FIELD] = {"sources": [row]}
    monkeypatch.setattr(
        routes.storage, "object_metadata", lambda path: SimpleNamespace(generation="42")
    )
    await routes.validate_editor_sources(
        a.db, job=a.job, variant=a.variant, used_media_ids={"clip"}
    )
    a.db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_save_visual_checks_kind_and_generation(admission):
    a = admission
    row = {
        **source(str(a.asset.id), lane="asset", kind="image", duration_s=None),
        "status": "ready",
    }
    row["visual_binding"] = {
        "media_id": str(a.asset.id),
        "kind": "video",
        "generation": "42",
        "gcs_path": a.asset.gcs_path,
    }
    a.variant[EDITOR_SOURCES_FIELD] = {"sources": [row]}
    with pytest.raises(HTTPException, match="editor_visual_unavailable"):
        await routes.validate_editor_sources(
            a.db, job=a.job, variant=a.variant, used_media_ids={str(a.asset.id)}
        )


@pytest.mark.asyncio
async def test_existing_bound_footage_reuses_index_without_consumed_reservation(
    admission, monkeypatch
):
    from app.services.phone_sources import PHONE_SOURCES_FIELD, bind_phone_sources
    from tests.services.test_phone_sources import receipt

    a = admission
    raw = receipt("approved")
    binding = bind_phone_sources([raw], [raw["gcs_path"]])[0]
    canonical = source(
        "approved", gcs_path=binding.proxy_path, generation=binding.generation, duration_s=10
    )
    a.job.assembly_plan[PHONE_SOURCES_FIELD] = [binding.model_dump(mode="json")]
    monkeypatch.setattr(
        routes, "_guided_v2_revision", lambda *args: {"revision_number": 1, "sources": [canonical]}
    )
    out, response = await post(
        a, a.body.model_copy(update={"source_kind": "footage", "source_id": "approved"})
    )
    assert out.status == "ready" and out.source_index == 0 and response.status_code == 200
    assert out.source.model_dump(mode="json") == canonical
    assert a.variant[EDITOR_SOURCES_FIELD]["sources"] == []
    a.publish.assert_not_called()
    assert a.db.execute.call_count == 1  # Job only, no reservation needed.


@pytest.mark.asyncio
async def test_save_accepts_real_photo_receipt_with_omitted_default_kind(admission):
    from app.services.phone_sources import PhoneVisualBinding

    a = admission
    receipt = PhoneVisualBinding(
        media_id=str(a.asset.id),
        gcs_path=a.asset.gcs_path,
        generation=a.asset.gcs_generation,
        sha256="a" * 64,
        byte_count=1234,
    ).model_dump(mode="json")
    assert "kind" not in receipt  # Backward-compatible persisted photo shape.
    a.variant[EDITOR_SOURCES_FIELD] = {
        "sources": [
            {
                **source(
                    str(a.asset.id),
                    lane="asset",
                    kind="image",
                    duration_s=None,
                    gcs_path=a.asset.gcs_path,
                    generation=a.asset.gcs_generation,
                ),
                "status": "ready",
                "visual_binding": receipt,
            }
        ]
    }
    await routes.validate_editor_sources(
        a.db, job=a.job, variant=a.variant, used_media_ids={str(a.asset.id)}
    )
    # The omitted kind means image, never "accept any current kind".
    a.asset.kind = "video"
    with pytest.raises(HTTPException, match="editor_visual_unavailable"):
        await routes.validate_editor_sources(
            a.db, job=a.job, variant=a.variant, used_media_ids={str(a.asset.id)}
        )
