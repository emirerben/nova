import copy
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException, Response
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import DBAPIError

from app.models import CreationThread, CreationThreadUploadReservation, Job, PlanItem, PlanItemAsset
from app.routes import editor_sources as routes
from app.services.phone_editor_sources import EDITOR_SOURCES_FIELD
from app.tasks import editor_sources as task
from tests.services.test_phone_editor_sources import admitted, source
from tests.services.test_phone_sources import receipt


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
    owner_connection = SimpleNamespace(execute=AsyncMock())
    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: job)),
        get=AsyncMock(side_effect=lambda model, *a, **kw: job if model is Job else asset),
        connection=AsyncMock(return_value=owner_connection),
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


async def post(a, body=None, *, native_client=False):
    response = Response()
    out = await routes.create_editor_source(
        str(a.item.id), "v", body or a.body, a.user, response, a.db, native_client=native_client
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
async def test_visual_admission_locks_asset_before_job(admission, monkeypatch):
    a = admission
    locks = []

    async def load_item(*args, **kwargs):
        assert kwargs["for_update"]
        locks.append(PlanItem)
        return a.item

    async def load_asset(model, ident, **kwargs):
        assert model is PlanItemAsset and ident == a.asset.id
        assert kwargs == {"with_for_update": True, "populate_existing": True}
        locks.append(model)
        return a.asset

    async def load_job(statement):
        assert "FOR UPDATE" in str(statement.compile(dialect=postgresql.dialect()))
        locks.append(Job)
        return SimpleNamespace(scalar_one_or_none=lambda: a.job)

    monkeypatch.setattr(routes, "_load_owned_item", load_item)
    a.db.connection.return_value.execute.side_effect = lambda query: locks.append("owner")
    a.db.get = AsyncMock(side_effect=load_asset)
    a.db.execute = AsyncMock(side_effect=load_job)
    await post(a)
    assert locks == ["owner", PlanItem, PlanItemAsset, Job]
    a.db.connection.assert_awaited_once()


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
    assert "FOR UPDATE OF creation_thread_upload_reservations NOWAIT" in query


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
# Value card: protects=ready-footage identity and receipt reuse for native library sources;
# fails_when=requested library asset/path/generation/original descriptor differs from the bound
# source; why_new=checks the real route before reference, commit, or publish side effects;
# seam=none; no_claim=does not cover worker probing after a new upload.
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

    from app.services import media_library_ingest as ingest

    monkeypatch.setattr(routes.settings, "media_library_enabled", True)
    library_asset_id = uuid.uuid4()
    library_source_id = uuid.uuid4()
    library_asset = SimpleNamespace(
        id=library_asset_id,
        user_id=a.user.id,
        metadata_json={"upload_contract": raw["upload_contract"]},
    )
    library_source = SimpleNamespace(
        id=library_source_id,
        asset_id=library_asset_id,
        user_id=a.user.id,
        role="analysis_proxy",
        status="available",
        object_path=binding.proxy_path,
        object_generation=binding.generation,
    )
    monkeypatch.setattr(ingest, "lock_library_owner", AsyncMock())
    resolve = AsyncMock(return_value=(library_asset, library_source))
    reference = AsyncMock()
    monkeypatch.setattr(ingest, "resolve_library_source", resolve)
    monkeypatch.setattr(ingest, "reference_library_source", reference)

    ready_body = a.body.model_copy(
        update={
            "client_import_id": uuid.uuid4(),
            "source_kind": "footage",
            "source_id": "approved",
            "library_asset_id": library_asset_id,
            "library_source_id": library_source_id,
        }
    )
    a.db.commit.reset_mock()
    out, response = await post(a, ready_body, native_client=True)
    assert out.status == "ready" and response.status_code == 200
    assert out.source_index == 0
    assert a.db.commit.await_count == 1
    assert reference.await_count == 1
    a.db.commit.reset_mock()
    reference.reset_mock()
    a.publish.reset_mock()

    mismatches = (
        (
            "path",
            SimpleNamespace(**{**library_source.__dict__, "object_path": "other/proxy.mp4"}),
            library_asset,
            {},
        ),
        (
            "generation",
            SimpleNamespace(**{**library_source.__dict__, "object_generation": "999"}),
            library_asset,
            {},
        ),
        (
            "original",
            library_source,
            SimpleNamespace(
                **{
                    **library_asset.__dict__,
                    "metadata_json": {
                        "upload_contract": {
                            **raw["upload_contract"],
                            "proxy": {
                                **raw["upload_contract"]["proxy"],
                                "original": {
                                    **raw["upload_contract"]["proxy"]["original"],
                                    "sha256": "b" * 64,
                                },
                            },
                        }
                    },
                }
            ),
            {},
        ),
    )
    for label, candidate_source, candidate_asset, body_update in mismatches:
        resolve.return_value = (candidate_asset, candidate_source)
        body = ready_body.model_copy(update={"client_import_id": uuid.uuid4(), **body_update})
        with pytest.raises(HTTPException, match="editor_source_identity_conflict"):
            await post(a, body, native_client=True)
        assert a.db.commit.await_count == 0, label
        reference.assert_not_called()
        a.publish.assert_not_called()


@pytest.mark.asyncio
async def test_footage_admission_runs_post_prepare_get_and_save_validation(admission, monkeypatch):
    """Exercise the real route, task, and durable-receipt save gate together.

    The boundary doubles are deliberately limited to DB/storage/ffprobe: route
    reservation ownership, task proxy validation, registry merge, readback,
    and the subsequent save-time receipt validation all remain real.
    """
    a = admission
    from app.routes import generative_jobs
    from tests.routes.test_phone_guided_timeline_commit import _guided_phone_job

    # Keep the actual approval projection: a mocked source helper previously
    # hid the gap between a ready admission and the public timeline read.
    a.job, a.variant, _ = _guided_phone_job(monkeypatch, count=1, clip_s=3)
    a.job.user_id = a.user.id
    a.job.content_plan_item_id = a.item.id
    a.item.current_job_id = a.job.id
    monkeypatch.setattr(routes, "_guided_v2_revision", generative_jobs._guided_v2_revision)
    monkeypatch.setattr(generative_jobs.settings, "phone_editor_media_enabled", True)
    monkeypatch.setattr(generative_jobs.settings, "visual_blocks_enabled", True)
    monkeypatch.setattr(
        generative_jobs.settings,
        "phone_render_verified_features",
        [
            *generative_jobs.settings.phone_render_verified_features,
            "stillImages",
            "visualVideos",
            "visualBlocks",
            "alphaOverlay",
        ],
    )
    raw = receipt("new-footage")
    thread = SimpleNamespace(id=uuid.uuid4(), creator_id=a.user.id, active_plan_item_id=a.item.id)
    reservation = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=a.user.id,
        thread_id=thread.id,
        object_path=raw["gcs_path"],
        media_id="new-footage",
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
        upload_contract=raw["upload_contract"],
    )
    # POST takes the job lock then the owned-reservation lock.
    a.db.execute = AsyncMock(
        side_effect=[
            SimpleNamespace(scalar_one_or_none=lambda: a.job),
            SimpleNamespace(scalar_one_or_none=lambda: reservation),
        ]
    )
    a.db.scalar = AsyncMock(return_value=None)
    a.db.get = AsyncMock(return_value=a.job)
    body = a.body.model_copy(update={"source_kind": "footage", "source_id": "new-footage"})
    response = Response()
    out = await routes.create_editor_source(
        str(a.item.id), "guided_story", body, a.user, response, a.db
    )
    assert (out.status, response.status_code) == ("preparing", 202)
    record = a.variant[EDITOR_SOURCES_FIELD]["imports"][str(body.client_import_id)]
    assert record["reservation_id"] == str(reservation.id)

    objects = {
        Job: a.job,
        PlanItem: a.item,
        CreationThread: thread,
        CreationThreadUploadReservation: reservation,
    }

    class Session:
        def get(self, model, *args, **kwargs):
            return objects.get(model)

        def execute(self, query):
            if "pg_advisory_xact_lock" in str(query.compile(dialect=postgresql.dialect())):
                return SimpleNamespace(scalar_one_or_none=lambda: None)
            model = query.column_descriptions[0]["entity"]
            return SimpleNamespace(scalar_one_or_none=lambda: objects.get(model))

        def scalar(self, query):
            # This fixture intentionally models a reservation without a
            # persisted cleanup receipt; the task must still admit it.
            return None

        def commit(self):
            return None

    @contextmanager
    def session():
        yield Session()

    probe = SimpleNamespace(
        width=640,
        height=360,
        fps=15,
        rotation_degrees=0,
        duration_s=10,
        has_audio=True,
        codec="h264",
        pix_fmt="yuv420p",
    )
    monkeypatch.setattr(task, "sync_session", session)
    monkeypatch.setattr(task, "flag_modified", Mock())

    def signed_url(*args, **kwargs):
        return "signed"

    def metadata(path):
        return SimpleNamespace(generation="42", size=1000)

    monkeypatch.setattr(task.storage, "signed_get_url_for_generation", signed_url)
    monkeypatch.setattr(task.storage, "object_metadata", metadata)
    monkeypatch.setattr(routes.storage, "object_metadata", metadata)
    monkeypatch.setattr(task, "probe_video", lambda url: probe)
    task.prepare_phone_editor_source.run(
        str(a.job.id), "guided_story", str(body.client_import_id), record["attempt_id"]
    )

    readback = await routes.get_editor_source(
        str(a.item.id), "guided_story", body.client_import_id, a.user, a.db
    )
    assert readback.status == "ready", readback.reason_code
    assert readback.source_index == 1
    assert readback.source.media_id == "new-footage"
    assert readback.source.gcs_path == raw["gcs_path"]
    assert reservation.expires_at <= datetime.now(UTC)
    await routes.validate_editor_sources(
        a.db, job=a.job, variant=a.variant, used_media_ids={"new-footage"}
    )
    opened = generative_jobs.dispatch_get_timeline(
        a.job, "guided_story", sign_url=lambda path, ttl: "signed"
    )
    imported = opened["clips"][readback.source_index]
    assert imported["media_id"] == readback.source.media_id
    from app.kria.media_sources import OriginalMediaDescriptor

    assert imported["native_source"]["original"] == OriginalMediaDescriptor.model_validate(
        raw["upload_contract"]["proxy"]["original"]
    ).model_dump(mode="json")
    registry = copy.deepcopy(a.variant[EDITOR_SOURCES_FIELD])
    generative_jobs.prepare_editor_commit(
        a.job,
        "guided_story",
        generative_jobs.EditorCommitRequest(
            base_generation=opened["base_generation"],
            guided_revision_number=opened["revision_number"],
            timeline_slots=[
                generative_jobs.TimelineSlotEdit(
                    slot_id=opened["slots"][0]["slot_id"], clip_index=0, in_s=0, duration_s=3
                ),
                generative_jobs.TimelineSlotEdit(
                    slot_id=str(uuid.uuid4()),
                    clip_index=readback.source_index,
                    in_s=0,
                    duration_s=2,
                ),
            ],
        ),
    )
    a.job.assembly_plan = copy.deepcopy(a.job.assembly_plan)
    reopened = generative_jobs.dispatch_get_timeline(
        a.job, "guided_story", sign_url=lambda path, ttl: "signed"
    )
    assert reopened["slots"][-1]["clip_index"] == readback.source_index
    assert reopened["clips"][readback.source_index]["used"] is True
    assert a.job.assembly_plan["variants"][0][EDITOR_SOURCES_FIELD] == registry


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


@pytest.mark.asyncio
@pytest.mark.parametrize("attribute", ["pgcode", "sqlstate"])
async def test_reservation_nowait_conflict_releases_transaction_and_is_retryable(
    admission, attribute
):
    a = admission
    original = Exception("private lock owner and SQL detail")
    setattr(original, attribute, "55P03")
    a.db.execute.side_effect = DBAPIError("private SQL", {}, original)
    a.db.rollback = AsyncMock()
    with pytest.raises(HTTPException) as exc:
        await routes._owned_proxy_reservation(
            a.db, user_id=a.user.id, source_id="clip", item_id=a.item.id
        )
    assert exc.value.status_code == 503
    assert exc.value.detail == {"code": "source_reservation_busy", "retryable": True}
    a.db.rollback.assert_awaited_once()
    a.db.commit.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["23505", "40P01", None])
async def test_other_reservation_database_failures_are_not_disguised_as_busy(admission, code):
    a = admission
    original = Exception("unrelated database fault")
    original.sqlstate = code
    failure = DBAPIError("query", {}, original)
    a.db.execute.side_effect = failure
    a.db.rollback = AsyncMock()
    with pytest.raises(DBAPIError) as exc:
        await routes._owned_proxy_reservation(
            a.db, user_id=a.user.id, source_id="clip", item_id=a.item.id
        )
    assert exc.value is failure
    a.db.rollback.assert_not_called()
