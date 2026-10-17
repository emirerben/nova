"""Real PostgreSQL reuse across independent posts, with immutable native receipts."""

import hashlib
import io
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from PIL import Image
from sqlalchemy import delete, select
from starlette.requests import Request

from app import storage
from app.database import AsyncSessionLocal, engine, sync_session
from app.models import (
    ContentPlan,
    CreationThread,
    CreationThreadUploadReservation,
    Job,
    MediaAsset,
    MediaAssetReference,
    MediaAssetSource,
    Persona,
    PlanItem,
    PlanItemAsset,
    TemporaryMediaUpload,
    User,
)
from app.pipeline import clip_cache
from app.routes import creation_threads as threads
from app.services import media_library_analysis as facts
from app.services import media_library_ingest as ingest
from app.services.media_library_projections import project_library_visual
from app.services.phone_sources import bind_phone_sources
from app.services.phone_visuals import bind_phone_visual_assets
from app.tasks import editor_sources as editor
from tests.services.test_phone_sources import receipt

if not os.environ.get("DATABASE_URL", "").rsplit("/", 1)[-1].endswith("_test"):
    pytest.skip("Library reuse requires a disposable database", allow_module_level=True)


@pytest.fixture(autouse=True)
async def dispose_pool():
    # A preceding module may leave connections bound to its finished event loop.
    await engine.dispose(close=False)
    yield
    await engine.dispose()


@pytest.fixture
async def world(monkeypatch):
    owner, persona, plan = (uuid.uuid4() for _ in range(3))
    items, jobs, chats = ([uuid.uuid4() for _ in range(2)] for _ in range(3))
    png = io.BytesIO()
    Image.new("RGB", (8, 8), "blue").save(png, format="PNG")
    data = png.getvalue()
    monkeypatch.setattr(ingest.settings, "media_library_enabled", True)
    monkeypatch.setattr(clip_cache, "_get_redis", lambda: None)
    async with AsyncSessionLocal() as db:
        db.add(User(id=owner, email=f"{owner}@library-reuse.test"))
        await db.flush()
        db.add(Persona(id=persona, user_id=owner, persona={}))
        await db.flush()
        db.add(ContentPlan(id=plan, persona_id=persona, user_id=owner))
        await db.flush()
        for index in range(2):
            db.add(
                PlanItem(
                    id=items[index], content_plan_id=plan, position=index, idea="Original idea"
                )
            )
            await db.flush()
            db.add(
                Job(
                    id=jobs[index],
                    user_id=owner,
                    content_plan_item_id=items[index],
                    raw_storage_path="",
                    status="ready",
                )
            )
            db.add(
                CreationThread(
                    id=chats[index],
                    creator_id=owner,
                    content_plan_id=plan,
                    active_plan_item_id=items[index],
                )
            )
        image, source = await ingest.register_verified_source(
            db,
            owner_id=owner,
            kind="image",
            object_path=f"users/{owner}/media-library/photo.png",
            generation="42",
            uploaded_sha256=hashlib.sha256(data).hexdigest(),
            byte_count=len(data),
            content_type="image/png",
            role="original",
            metadata={"filename": "photo.png"},
        )
        proxy_contract = receipt()["upload_contract"]
        video, proxy = await ingest.register_verified_source(
            db,
            owner_id=owner,
            kind="video",
            object_path=f"users/{owner}/media-library/analysis-proxy-video.mp4",
            generation="43",
            uploaded_sha256=hashlib.sha256(data).hexdigest(),
            byte_count=len(data),
            content_type="video/mp4",
            role="analysis_proxy",
            original_declared_sha256="a" * 64,
            original_byte_count=1000,
            metadata={"upload_contract": proxy_contract, "duration_s": 10, "has_audio": True},
        )
        image_id, source_id, video_id, proxy_id = image.id, source.id, video.id, proxy.id
        await db.commit()
    downloads, providers = [], []

    def metadata(path):
        return SimpleNamespace(
            generation="43" if "proxy" in path else "42",
            size=12 if "proxy" in path else len(data),
            content_type="video/mp4" if "proxy" in path else "image/png",
        )

    monkeypatch.setattr(storage, "object_metadata", metadata)
    real_analyze_source = facts._analyze_source
    monkeypatch.setattr(
        storage,
        "download_generation_to_file",
        lambda path, local, **kw: downloads.append((path, kw)) or Path(local).write_bytes(data),
    )
    monkeypatch.setattr(
        storage, "signed_get_url_for_generation", lambda path, **kw: "https://source.test/pinned"
    )
    monkeypatch.setattr(
        facts,
        "_analyze_source",
        lambda source: (
            providers.append(source.kind)
            or {"source": "provider", "description": "Blue photo", "analysis_version": 8}
        ),
    )
    monkeypatch.setattr(threads, "_ensure_speech_cleanup_preflight", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "app.tasks.kria_clip_understanding.enqueue_clip_understanding", lambda *a: None
    )
    yield SimpleNamespace(
        owner=owner,
        items=items,
        jobs=jobs,
        chats=chats,
        image=image_id,
        source=source_id,
        video=video_id,
        proxy=proxy_id,
        data=data,
        downloads=downloads,
        providers=providers,
        real_analyze_source=real_analyze_source,
    )
    async with AsyncSessionLocal() as db:
        await db.execute(delete(Job).where(Job.user_id == owner))
        await db.execute(delete(PlanItemAsset).where(PlanItemAsset.user_id == owner))
        await db.execute(delete(User).where(User.id == owner))
        await db.commit()


@pytest.mark.asyncio
async def test_photo_reuse_edit_archive_and_native_hash_verification(world, monkeypatch):
    w = world
    from app.services.slide_post_chat_edit import _asset_upright_aspect
    from app.tasks import autoplace

    monkeypatch.setattr(
        autoplace,
        "analyze_pool_image",
        lambda *args, **kwargs: (
            {"source": "provider", "description": "Blue photo"},
            1.0,
            (8, 8),
            False,
        ),
    )

    def real_provider(source, **kwargs):
        w.providers.append(source.kind)
        return w.real_analyze_source(source, **kwargs)

    monkeypatch.setattr(facts, "_analyze_source", real_provider)
    assert facts.ensure_media_asset_analysis(asset_id=str(w.image), owner_id=str(w.owner))
    assert len(w.downloads) == 1
    w.downloads.clear()
    projected = []
    async with AsyncSessionLocal() as db:
        for item in w.items:
            row = await project_library_visual(db, owner_id=w.owner, item_id=item, asset_id=w.image)
            projected.append(row.id)
            await db.commit()
        assert len(set(projected)) == 2 and w.providers == ["image"] and w.downloads == []
        first = await db.get(PlanItemAsset, projected[0])
        first.user_context = "Use in the opening"
        await db.commit()
        second = await db.get(PlanItemAsset, projected[1])
        assert second.user_context == "" and second.gcs_path == first.gcs_path
        assert first.aspect == second.aspect == 1.0
        assert first.analysis["aspect"] == second.analysis["aspect"] == 1.0
        assert _asset_upright_aspect(first) == _asset_upright_aspect(second) == 1.0
        # Uploading a third unique asset automatically removes the oldest saved
        # entry, even while both posts reference it. Their pins must survive.
        monkeypatch.setattr(ingest, "ACTIVE_ASSET_LIMIT", 2)
        image = await db.get(MediaAsset, w.image)
        image.created_at = datetime.now(UTC) - timedelta(days=1)
        await db.commit()
        await ingest.register_verified_source(
            db,
            owner_id=w.owner,
            kind="audio",
            object_path=f"users/{w.owner}/media-library/new-song.mp3",
            generation="44",
            uploaded_sha256="f" * 64,
            byte_count=12,
            content_type="audio/mpeg",
            role="original",
        )
        await db.commit()
        assert (await db.get(MediaAsset, w.image)).archived_at is not None
        with pytest.raises(HTTPException):
            await project_library_visual(db, owner_id=w.owner, item_id=w.items[0], asset_id=w.image)
        await db.rollback()
    path = f"users/{w.owner}/media-library/photo.png"
    bound = bind_phone_visual_assets(
        sync_session, job_id=str(w.jobs[1]), pins={str(projected[1]): ("image", path, "42")}
    )
    assert bound[0].sha256 == hashlib.sha256(w.data).hexdigest()
    monkeypatch.setattr(
        storage,
        "download_generation_to_file",
        lambda path, local, **kw: Path(local).write_bytes(b"other bytes"),
    )
    with pytest.raises(ValueError, match="verified identity"):
        bind_phone_visual_assets(
            sync_session, job_id=str(w.jobs[1]), pins={str(projected[1]): ("image", path, "42")}
        )
    async with AsyncSessionLocal() as db:
        refs = (
            await db.scalars(
                select(MediaAssetReference).where(MediaAssetReference.asset_id == w.image)
            )
        ).all()
        assert {ref.plan_item_id for ref in refs} == set(w.items)
    print(
        "posts=2 source_analysis_calls=1 upload_calls=0 independent_edit_state=verified "
        "automatic_rotation_existing_render=verified"
    )


@pytest.mark.asyncio
async def test_proxy_binding_cannot_use_legacy_pool_namespace(world, monkeypatch):
    w = world
    async with AsyncSessionLocal() as db:
        projected = await project_library_visual(
            db, owner_id=w.owner, item_id=w.items[0], asset_id=w.image
        )
        projected.library_asset_id, projected.library_source_id = w.video, w.proxy
        projected.kind, projected.status = "video", "ready"
        projected.gcs_path = f"users/{w.owner}/plan/{w.items[0]}/pool/proxy.mp4"
        projected.gcs_generation = "43"
        pin = ("video", projected.gcs_path, "43")
        identifier = str(projected.id)
        await db.commit()

    def forbidden_download(*args, **kwargs):
        raise AssertionError("A proxy reached native-original download")

    monkeypatch.setattr(storage, "download_generation_to_file", forbidden_download)
    with pytest.raises(ValueError, match="ready visual"):
        bind_phone_visual_assets(sync_session, job_id=str(w.jobs[0]), pins={identifier: pin})


@pytest.mark.asyncio
# Value card: protects=canonical library source binding and cleanup isolation for legacy audio
# duplicates; fails_when=second song/voiceover uploads retain their own paths or lack a cleanup
# receipt after coalescing; why_new=exercises native audio admission, retry idempotency, and
# remove-media path resolution for both audio lanes; seam=none; no_claim=does not cover cloud
# deletion workers or provider analysis.
async def test_two_chats_reuse_proxy_but_bind_the_original(world, monkeypatch):
    w = world
    for index, chat in enumerate(w.chats):
        async with AsyncSessionLocal() as db:
            body = threads.AttachBody(
                client_event_id="reuse",
                expected_revision=0,
                media=[
                    threads.MediaInput(
                        media_id=f"analysis-proxy-post-{index}.mp4",
                        kind="video",
                        library_asset_id=w.video,
                    )
                ],
            )
            request = Request(
                {
                    "type": "http",
                    "method": "POST",
                    "path": f"/creation-threads/{chat}/media",
                    "headers": [],
                }
            )
            await threads.attach_media(
                request,
                str(chat),
                body,
                SimpleNamespace(id=w.owner),
                db,
                native_client=True,
                client_protocol=2,
            )
            # Stable retry creates no second assignment or usage reference.
            await threads.attach_media(
                request,
                str(chat),
                body,
                SimpleNamespace(id=w.owner),
                db,
                native_client=True,
                client_protocol=2,
            )
    async with AsyncSessionLocal() as db:
        for index, item_id in enumerate(w.items):
            item = await db.get(PlanItem, item_id)
            assert len(item.clip_assignments) == 1
            assert item.clip_assignments[0]["media_id"] == f"analysis-proxy-post-{index}.mp4"
            bound = bind_phone_sources(item.clip_assignments, item.clip_gcs_paths)
            assert bound[0].original.sha256 == "a" * 64
            original = bound[0].render_asset().model_dump(mode="json")
            assert original["kind"] == "original" and "gcs_path" not in original
            assert original["fingerprint"]["sha256"] == "a" * 64
        refs = (
            await db.scalars(
                select(MediaAssetReference).where(MediaAssetReference.asset_id == w.video)
            )
        ).all()
        assert {ref.creation_thread_id for ref in refs} == set(w.chats)
        with pytest.raises(HTTPException):
            await project_library_visual(db, owner_id=w.owner, item_id=w.items[0], asset_id=w.video)
        item = await db.get(PlanItem, w.items[0], with_for_update=True)
        item.edit_format = "montage"
        await db.commit()
        thread = await db.get(CreationThread, w.chats[0])
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": f"/creation-threads/{w.chats[0]}/media",
                "headers": [],
            }
        )
        before_assignments = list(item.clip_assignments)
        before_state = dict(thread.state)
        before_ref_count = len(refs)
        reselection = threads.AttachBody(
            client_event_id="reselect-existing-library-source",
            expected_revision=thread.revision,
            media=[
                threads.MediaInput(
                    media_id="analysis-proxy-reselected.mp4",
                    kind="video",
                    library_asset_id=w.video,
                    library_source_id=w.proxy,
                )
            ],
        )
        with pytest.raises(HTTPException) as error:
            await threads.attach_media(
                request,
                str(w.chats[0]),
                reselection,
                SimpleNamespace(id=w.owner),
                db,
                native_client=True,
                client_protocol=2,
            )
        assert error.value.status_code == 409
        await db.refresh(item)
        await db.refresh(thread)
        assert item.clip_assignments == before_assignments
        assert thread.state == before_state
        refs = (
            await db.scalars(
                select(MediaAssetReference).where(MediaAssetReference.asset_id == w.video)
            )
        ).all()
        assert len(refs) == before_ref_count

        thread = await db.get(CreationThread, w.chats[0])
        batch = threads.AttachBody(
            client_event_id="same-batch-library-source",
            expected_revision=thread.revision,
            media=[
                threads.MediaInput(
                    media_id="analysis-proxy-batch-a.mp4",
                    kind="video",
                    library_asset_id=w.video,
                    library_source_id=w.proxy,
                ),
                threads.MediaInput(
                    media_id="analysis-proxy-batch-b.mp4",
                    kind="video",
                    library_asset_id=w.video,
                    library_source_id=w.proxy,
                ),
            ],
        )
        with pytest.raises(HTTPException) as error:
            await threads.attach_media(
                request,
                str(w.chats[0]),
                batch,
                SimpleNamespace(id=w.owner),
                db,
                native_client=True,
                client_protocol=2,
            )
        assert error.value.status_code == 409
        await db.refresh(item)
        await db.refresh(thread)
        assert item.clip_assignments == before_assignments
        assert thread.state == before_state
        refs = (
            await db.scalars(
                select(MediaAssetReference).where(MediaAssetReference.asset_id == w.video)
            )
        ).all()
        assert len(refs) == before_ref_count
    assert w.downloads == []
    print("chats=2 uploads=0 source_downloads=0 original_bindings=2 idempotent_retries=2")

    # Value card: protects=legacy native proxy re-imports roll back without
    # leaking a physical upload; fails_when=an already-known proxy is selected
    # through a fresh reservation and the abandoned path has no durable cleanup
    # receipt; why_new=exercises the upload-url/reservation/attach seam with
    # real bytes and a real source coalescing decision; seam=none;
    # no_claim=does not cover cleanup workers or cloud deletion.
    proxy_bytes = w.data
    proxy_contract = receipt()["upload_contract"]
    signed_calls = []
    monkeypatch.setattr(type(threads.settings), "phone_rendering_for", lambda _self, _uid: True)
    monkeypatch.setattr(
        storage,
        "object_metadata",
        lambda path: SimpleNamespace(
            generation="43",
            size=len(proxy_bytes) if "proxy" in path else len(w.data),
            content_type="video/mp4" if "proxy" in path else "image/png",
        ),
    )
    monkeypatch.setattr(
        storage,
        "download_generation_to_file",
        lambda path, local, **kw: (
            w.downloads.append((path, kw)) or Path(local).write_bytes(proxy_bytes)
        ),
    )
    monkeypatch.setattr(
        ingest.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout=json.dumps(
                {
                    "format": {"duration": "10"},
                    "streams": [
                        {
                            "codec_type": "video",
                            "codec_name": "h264",
                            "width": 640,
                            "height": 360,
                            "avg_frame_rate": "15/1",
                            "tags": {"rotate": "0"},
                        },
                        {"codec_type": "audio"},
                    ],
                }
            )
        ),
    )
    monkeypatch.setattr(threads, "_probe_registered_media", AsyncMock(return_value=(10.0, True)))

    async def reserve_proxy(chat_id, client_id):
        async with AsyncSessionLocal() as db:
            body = threads.UploadBody(
                files=[
                    threads.UploadFile(
                        filename="legacy-proxy.mp4",
                        content_type="video/mp4",
                        file_size_bytes=len(proxy_bytes),
                        client_upload_id=client_id,
                        upload_contract=proxy_contract,
                    )
                ]
            )
            monkeypatch.setattr(
                storage,
                "signed_put_url",
                lambda path, *args: signed_calls.append(path) or f"signed:{path}",
            )
            request = Request({"type": "http", "method": "POST", "path": "", "headers": []})
            request.state.view_rate_limit = None
            targets = await threads.upload_urls(
                request,
                str(chat_id),
                body,
                SimpleNamespace(id=w.owner),
                db,
                native_client=True,
            )
            assert targets[0].gcs_path == threads._media_path(w.owner, chat_id, targets[0].media_id)
            return targets[0]

    target = await reserve_proxy(w.chats[0], "legacy-reimport")
    async with AsyncSessionLocal() as db:
        reservation = await db.scalar(
            select(CreationThreadUploadReservation).where(
                CreationThreadUploadReservation.thread_id == w.chats[0],
                CreationThreadUploadReservation.media_id == target.media_id,
            )
        )
        assert reservation is not None
        assert reservation.upload_contract["proxy"]["original"]["sha256"] == "a" * 64
        item_before = await db.get(PlanItem, w.items[0])
        thread_before = await db.get(CreationThread, w.chats[0])
        assignments_before = list(item_before.clip_assignments)
        state_before = dict(thread_before.state)
        refs_before = len(
            (
                await db.scalars(
                    select(MediaAssetReference).where(MediaAssetReference.asset_id == w.video)
                )
            ).all()
        )
        with pytest.raises(HTTPException) as failure:
            await threads.attach_media(
                Request({"type": "http", "method": "POST", "path": "", "headers": []}),
                str(w.chats[0]),
                threads.AttachBody(
                    client_event_id="legacy-reimport",
                    expected_revision=thread_before.revision,
                    media=[
                        threads.MediaInput(
                            media_id=target.media_id,
                            kind="video",
                            filename="legacy-proxy.mp4",
                            content_type="video/mp4",
                        )
                    ],
                ),
                SimpleNamespace(id=w.owner),
                db,
                native_client=True,
                client_protocol=2,
            )
        assert failure.value.status_code == 409
        await db.rollback()
    async with AsyncSessionLocal() as db:
        item_after = await db.get(PlanItem, w.items[0])
        thread_after = await db.get(CreationThread, w.chats[0])
        assert item_after.clip_assignments == assignments_before
        assert thread_after.state == state_before
        refs_after = (
            await db.scalars(
                select(MediaAssetReference).where(MediaAssetReference.asset_id == w.video)
            )
        ).all()
        assert len(refs_after) == refs_before
        abandoned = (
            await db.scalars(
                select(TemporaryMediaUpload).where(
                    TemporaryMediaUpload.user_id == w.owner,
                    TemporaryMediaUpload.object_path == target.gcs_path,
                )
            )
        ).all()
        assert len(abandoned) == 1
    w.downloads.clear()

    batch_targets = [
        await reserve_proxy(w.chats[0], "legacy-batch-a"),
        await reserve_proxy(w.chats[0], "legacy-batch-b"),
    ]
    async with AsyncSessionLocal() as db:
        thread = await db.get(CreationThread, w.chats[0])
        with pytest.raises(HTTPException) as failure:
            await threads.attach_media(
                Request({"type": "http", "method": "POST", "path": "", "headers": []}),
                str(w.chats[0]),
                threads.AttachBody(
                    client_event_id="legacy-physical-batch",
                    expected_revision=thread.revision,
                    media=[
                        threads.MediaInput(
                            media_id=target.media_id,
                            kind="video",
                            filename="legacy-proxy.mp4",
                            content_type="video/mp4",
                        )
                        for target in batch_targets
                    ],
                ),
                SimpleNamespace(id=w.owner),
                db,
                native_client=True,
                client_protocol=2,
            )
        assert failure.value.status_code == 409
        await db.rollback()
    async with AsyncSessionLocal() as db:
        batch_receipts = (
            await db.scalars(
                select(TemporaryMediaUpload).where(
                    TemporaryMediaUpload.user_id == w.owner,
                    TemporaryMediaUpload.object_path.in_(
                        [target.gcs_path for target in batch_targets]
                    ),
                )
            )
        ).all()
        assert {row.object_path for row in batch_receipts} == {
            target.gcs_path for target in batch_targets
        }
        all_rejected_paths = [target.gcs_path for target in batch_targets]
        single_receipt = await db.scalar(
            select(TemporaryMediaUpload).where(
                TemporaryMediaUpload.user_id == w.owner,
                TemporaryMediaUpload.object_path.like("%legacy-reimport%"),
            )
        )
        assert single_receipt is not None
        reservation = await db.scalar(
            select(CreationThreadUploadReservation).where(
                CreationThreadUploadReservation.thread_id == w.chats[0],
                CreationThreadUploadReservation.media_id == target.media_id,
            )
        )
        reservation.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        single_receipt.retention_expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await db.commit()
    renewed_target = await reserve_proxy(w.chats[0], "legacy-reimport")
    assert renewed_target.gcs_path == target.gcs_path
    async with AsyncSessionLocal() as db:
        renewed_reservation = await db.scalar(
            select(CreationThreadUploadReservation).where(
                CreationThreadUploadReservation.thread_id == w.chats[0],
                CreationThreadUploadReservation.media_id == target.media_id,
            )
        )
        renewed_receipt = await db.scalar(
            select(TemporaryMediaUpload).where(
                TemporaryMediaUpload.user_id == w.owner,
                TemporaryMediaUpload.object_path == target.gcs_path,
            )
        )
        assert renewed_reservation.expires_at > datetime.now(UTC)
        assert renewed_receipt.retention_expires_at > renewed_reservation.expires_at
        all_rejected_paths.append(single_receipt.object_path)
        cleanup_receipts = (
            await db.scalars(
                select(TemporaryMediaUpload).where(
                    TemporaryMediaUpload.user_id == w.owner,
                    TemporaryMediaUpload.object_path.in_(all_rejected_paths),
                )
            )
        ).all()
        for row in cleanup_receipts:
            row.retention_expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await db.commit()

    from app.tasks import mobile_upload_cleanup

    deleted_paths = []
    monkeypatch.setattr(
        mobile_upload_cleanup,
        "delete_object_best_effort",
        lambda path: deleted_paths.append(path) or True,
    )
    assert mobile_upload_cleanup.cleanup_expired_temporary_uploads(now=datetime.now(UTC)) == len(
        all_rejected_paths
    )
    assert set(deleted_paths) == set(all_rejected_paths)
    assert {path for path, _kwargs in w.downloads} == {target.gcs_path for target in batch_targets}
    assert f"users/{w.owner}/media-library/analysis-proxy-video.mp4" not in deleted_paths
    signed_before_deleted_retry = len(signed_calls)
    with pytest.raises(HTTPException) as retry_failure:
        await reserve_proxy(w.chats[0], "legacy-reimport")
    assert retry_failure.value.status_code == 409
    assert len(signed_calls) == signed_before_deleted_retry

    audio_by_role = {
        "song": b"same uploaded audio bytes",
        "voiceover": b"same uploaded audio bytes",
    }
    audio_probe = SimpleNamespace(
        stdout=json.dumps({"format": {"duration": "10"}, "streams": [{"codec_type": "audio"}]}),
    )
    monkeypatch.setattr(
        storage,
        "object_metadata",
        lambda path: SimpleNamespace(
            generation="51",
            size=len(audio_by_role["voiceover" if "voiceover" in path else "song"]),
            content_type="audio/mp4",
        ),
    )
    monkeypatch.setattr(
        storage,
        "download_generation_to_file",
        lambda path, local, **kw: Path(local).write_bytes(
            audio_by_role["voiceover" if "voiceover" in path else "song"]
        ),
    )
    monkeypatch.setattr(ingest.subprocess, "run", lambda *args, **kwargs: audio_probe)
    monkeypatch.setattr(threads, "_probe_registered_media", AsyncMock(return_value=(10.0, True)))
    monkeypatch.setattr(threads, "_user_song_enabled", lambda *args, **kwargs: True)
    monkeypatch.setattr(storage, "delete_prefix_best_effort", lambda prefix: None)

    for role in ("song", "voiceover"):
        for index, chat in enumerate(w.chats):
            async with AsyncSessionLocal() as db:
                thread = await db.get(CreationThread, chat)
                body = threads.AttachBody(
                    client_event_id=f"audio-{role}-{index}",
                    expected_revision=thread.revision,
                    media=[
                        threads.MediaInput(
                            media_id=f"{role}-duplicate-{index}.m4a",
                            kind="audio",
                            filename=f"{role}.m4a",
                            content_type="audio/mp4",
                            role=role,
                        )
                    ],
                )
                request = Request(
                    {
                        "type": "http",
                        "method": "POST",
                        "path": f"/creation-threads/{chat}/media",
                        "headers": [],
                    }
                )
                await threads.attach_media(
                    request,
                    str(chat),
                    body,
                    SimpleNamespace(id=w.owner),
                    db,
                    native_client=True,
                    client_protocol=2,
                )
                await threads.attach_media(
                    request,
                    str(chat),
                    body,
                    SimpleNamespace(id=w.owner),
                    db,
                    native_client=True,
                    client_protocol=2,
                )

    for role in ("song", "voiceover"):
        async with AsyncSessionLocal() as db:
            canonical_path = threads._media_path(w.owner, w.chats[0], "song-duplicate-0.m4a")
            duplicate_path = threads._media_path(w.owner, w.chats[1], f"{role}-duplicate-1.m4a")
            for item_id in w.items:
                item = await db.get(PlanItem, item_id)
                assert (
                    item.song_gcs_path if role == "song" else item.voiceover_gcs_path
                ) == canonical_path
                assert (item.song_generation if role == "song" else item.voiceover_generation) == (
                    51 if role == "song" else "51"
                )
            asset_ids = {
                asset_id
                for (asset_id,) in (
                    await db.execute(
                        select(MediaAssetSource.asset_id).where(
                            MediaAssetSource.user_id == w.owner,
                            MediaAssetSource.uploaded_sha256
                            == hashlib.sha256(audio_by_role[role]).hexdigest(),
                        )
                    )
                ).all()
            }
            assert len(asset_ids) == 1
            asset = await db.get(MediaAsset, next(iter(asset_ids)))
            refs = (
                await db.scalars(
                    select(MediaAssetReference).where(MediaAssetReference.asset_id == asset.id)
                )
            ).all()
            assert len(refs) == 2
            receipts = (
                await db.scalars(
                    select(TemporaryMediaUpload).where(
                        TemporaryMediaUpload.user_id == w.owner,
                        TemporaryMediaUpload.object_path == duplicate_path,
                    )
                )
            ).all()
            assert len(receipts) == 1

            thread = await db.get(CreationThread, w.chats[1])
            remove = threads.ActionBody(
                action="remove_media",
                payload={"media_id": f"{role}-duplicate-1.m4a"},
                client_action_id=f"remove-{role}",
                expected_revision=thread.revision,
            )
            await threads.action_thread(
                Request(
                    {
                        "type": "http",
                        "method": "POST",
                        "path": f"/creation-threads/{w.chats[1]}/actions",
                        "headers": [],
                    }
                ),
                str(w.chats[1]),
                remove,
                SimpleNamespace(id=w.owner),
                db,
                native_client=True,
                client_protocol=2,
            )
            removed_item = await db.get(PlanItem, w.items[1])
            retained_item = await db.get(PlanItem, w.items[0])
            assert (
                removed_item.song_gcs_path if role == "song" else removed_item.voiceover_gcs_path
            ) is None
            assert (
                retained_item.song_gcs_path if role == "song" else retained_item.voiceover_gcs_path
            ) == canonical_path
            if role == "song":
                assert removed_item.voiceover_gcs_path == canonical_path
            else:
                assert removed_item.song_gcs_path is None
            remaining_refs = (
                await db.scalars(
                    select(MediaAssetReference).where(MediaAssetReference.asset_id == asset.id)
                )
            ).all()
            if role == "song":
                assert {ref.creation_thread_id for ref in remaining_refs} == set(w.chats)
            else:
                assert {ref.creation_thread_id for ref in remaining_refs} == {w.chats[0]}


@pytest.mark.asyncio
async def test_editor_library_footage_keeps_original_and_checks_owner_generation(
    world, monkeypatch
):
    w = world
    monkeypatch.setattr(
        editor,
        "probe_video",
        lambda url: SimpleNamespace(
            codec="h264",
            pix_fmt="yuv420p",
            width=640,
            height=360,
            fps=15,
            rotation_degrees=0,
            duration_s=10,
            has_audio=True,
        ),
    )
    record = {
        "library_asset_id": str(w.video),
        "_library_bound_source_id": str(w.proxy),
        "user_id": str(w.owner),
        "source_id": "analysis-proxy-editor.mp4",
    }
    # An existing post's bound source remains editable after automatic rotation.
    monkeypatch.setattr(ingest, "ACTIVE_ASSET_LIMIT", 2)
    async with AsyncSessionLocal() as db:
        video = await db.get(MediaAsset, w.video)
        video.created_at = datetime.now(UTC) - timedelta(days=1)
        await db.commit()
        await ingest.register_verified_source(
            db,
            owner_id=w.owner,
            kind="audio",
            object_path=f"users/{w.owner}/media-library/new-song.mp3",
            generation="44",
            uploaded_sha256="f" * 64,
            byte_count=12,
            content_type="audio/mpeg",
            role="original",
        )
        await db.commit()
        assert (await db.get(MediaAsset, w.video)).archived_at is not None
    source, binding = editor._prepare_footage(record)
    assert source["generation"] == "43" and binding.original.sha256 == "a" * 64
    with pytest.raises(editor.AdmissionError, match="library_source_unavailable"):
        editor._prepare_footage({**record, "user_id": str(uuid.uuid4())})
    monkeypatch.setattr(
        storage, "object_metadata", lambda path: SimpleNamespace(generation="44", size=12)
    )
    with pytest.raises(editor.AdmissionError, match="generation_changed"):
        editor._prepare_footage(record)
    assert w.downloads == []


@pytest.mark.asyncio
async def test_confirmed_erasure_removes_shared_post_projections_and_receipts(world, monkeypatch):
    from cryptography.fernet import Fernet

    from app.models import JobStorageDeletion
    from app.routes import me
    from app.tasks import account_lifecycle

    w = world
    async with AsyncSessionLocal() as db:
        for item in w.items:
            await project_library_visual(db, owner_id=w.owner, item_id=item, asset_id=w.image)
        await db.commit()
    key = Fernet.generate_key()
    monkeypatch.setattr(me.settings, "token_encryption_key", key.decode())
    dispatch = AsyncMock()
    monkeypatch.setattr(me, "_delete_job_storage_after_commit", dispatch)
    monkeypatch.setattr(account_lifecycle.purge_user_storage, "delay", lambda *a: None)
    token = Fernet(key).encrypt(str(w.owner).encode()).decode()
    async with AsyncSessionLocal() as db:
        user = await db.get(User, w.owner)
        response = await me.confirm_account_deletion(me.DeleteConfirmBody(token=token), user, db)
        assert response.status_code == 204
        assert await db.get(User, w.owner) is None
        assert await db.get(MediaAsset, w.image) is None
        assert not (
            await db.scalars(select(PlanItemAsset).where(PlanItemAsset.user_id == w.owner))
        ).all()
        outbox_ids = [call.args[0] for call in dispatch.call_args_list]
        for identifier in outbox_ids:
            await db.execute(delete(JobStorageDeletion).where(JobStorageDeletion.id == identifier))
        await db.commit()
