"""Library admission races against a migrated, disposable PostgreSQL database."""

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.exc import DBAPIError

from app.database import AsyncSessionLocal, engine
from app.models import Job, MediaAsset, TemporaryMediaUpload, User
from app.services import media_library_ingest as ingest

if not os.environ.get("DATABASE_URL", "").rsplit("/", 1)[-1].endswith("_test"):
    pytest.skip("Library ingest requires a disposable test database", allow_module_level=True)


@pytest.fixture(autouse=True)
async def _dispose_async_pool():
    # A preceding module may leave connections bound to its finished event loop.
    await engine.dispose(close=False)
    yield
    await engine.dispose()


@pytest.fixture
async def owner(monkeypatch):
    monkeypatch.setattr(ingest.settings, "media_library_enabled", True)
    monkeypatch.setattr(ingest, "dispatch_library_analysis", lambda *args: None)
    async with AsyncSessionLocal() as db:
        user = User(id=uuid.uuid4(), email=f"{uuid.uuid4()}@library-ingest.test")
        db.add(user)
        await db.commit()
        owner_id = user.id
    yield owner_id
    async with AsyncSessionLocal() as db:
        await db.execute(delete(User).where(User.id == owner_id))
        await db.commit()


def payload(client_id="import-1"):
    return dict(
        client_upload_id=client_id,
        filename="photo.png",
        kind="image",
        content_type="image/png",
        file_size_bytes=12,
        upload_contract={"purpose": "cloud_render_source", "proxy": None},
    )


@pytest.mark.asyncio
async def test_reservation_retry_and_changed_identity(owner):
    async with AsyncSessionLocal() as db:
        first = await ingest.reserve_library_upload(db, owner, payload())
        second = await ingest.reserve_library_upload(db, owner, payload())
        assert first.id == second.id
        with pytest.raises(HTTPException) as error:
            await ingest.reserve_library_upload(db, owner, {**payload(), "file_size_bytes": 13})
        assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_reimport_repairs_missing_cloud_copy_without_new_catalogue_slot(owner, monkeypatch):
    from google.api_core.exceptions import NotFound

    values = dict(
        owner_id=owner,
        kind="image",
        uploaded_sha256="f" * 64,
        byte_count=12,
        content_type="image/png",
        role="original",
    )
    async with AsyncSessionLocal() as db:
        first, old = await ingest.register_verified_source(
            db, object_path=f"users/{owner}/old.png", generation="1", **values
        )
        first_id, old_id = first.id, old.id
        await db.commit()
        monkeypatch.setattr(ingest, "ACTIVE_ASSET_LIMIT", 1)
        monkeypatch.setattr(
            ingest.storage, "object_metadata", lambda path: (_ for _ in ()).throw(NotFound("gone"))
        )
        repaired, fresh = await ingest.register_verified_source(
            db, object_path=f"users/{owner}/fresh.png", generation="2", **values
        )
        await db.commit()
        assert repaired.id == first_id
        assert fresh.id != old_id
        assert (await db.get(type(old), old_id)).status == "missing"
        assert fresh.object_generation == "2"


@pytest.mark.asyncio
@pytest.mark.parametrize("old_copy_exists", [True, False])
async def test_changed_proxy_repair_preserves_asset_at_capacity(
    owner, monkeypatch, old_copy_exists
):
    # Value card: protects=repaired default selection and immutable existing pins;
    # fails_when=oldest available ordering selects an old proxy after repair;
    # why_new=covers changed-hash repair across attach, analysis and catalogue at capacity;
    # seam=none; no_claim=does not render device originals or exercise actual cloud storage.
    from google.api_core.exceptions import NotFound

    from app.routes.media_assets import assets_out
    from app.services import media_library_analysis as facts
    from tests.services.test_phone_sources import receipt

    contract = receipt()["upload_contract"]
    async with AsyncSessionLocal() as db:
        asset, old = await ingest.register_verified_source(
            db,
            owner_id=owner,
            kind="video",
            object_path=f"users/{owner}/old-proxy.mp4",
            generation="1",
            uploaded_sha256="a" * 64,
            byte_count=12,
            content_type="video/mp4",
            role="analysis_proxy",
            original_declared_sha256=contract["proxy"]["original"]["sha256"],
            original_byte_count=contract["proxy"]["original"]["byte_count"],
            metadata={"upload_contract": contract},
        )
        identifier = asset.id
        await db.commit()
        monkeypatch.setattr(ingest, "ACTIVE_ASSET_LIMIT", 1)
        body = dict(
            client_upload_id="repair",
            filename="video.mp4",
            kind="video",
            content_type="video/mp4",
            file_size_bytes=12,
            upload_contract=contract,
            repair_asset_id=str(identifier),
        )
        reservation = await ingest.reserve_library_upload(db, owner, body)

        def metadata(path):
            if path.endswith("old-proxy.mp4"):
                if not old_copy_exists:
                    raise NotFound("gone")
                return SimpleNamespace(generation="1", size=12)
            return SimpleNamespace(generation="2", size=12)

        monkeypatch.setattr(ingest.storage, "object_metadata", metadata)
        monkeypatch.setattr(
            ingest,
            "verified_source_metadata",
            lambda *args: dict(
                uploaded_sha256="b" * 64,
                generation="2",
                byte_count=12,
                content_type="video/mp4",
                role="analysis_proxy",
                metadata={},
            ),
        )
        repaired, source = await ingest.finalize_library_upload(db, owner, reservation.id, "event")
        assert repaired.id == identifier and source.id != old.id
        count = (await db.scalars(select(MediaAsset).where(MediaAsset.user_id == owner))).all()
        assert len(count) == 1
        _, selected = await ingest.resolve_library_source(db, owner, identifier)
        assert selected.id == source.id
        _, pinned = await ingest.resolve_library_source(
            db, owner, identifier, old.id, allow_missing=True
        )
        assert pinned.id == old.id
        assert pinned.status == ("available" if old_copy_exists else "missing")
        requested = facts._requested_source(str(identifier), str(owner), None)
        assert requested.source_id == str(source.id)
        requested_pin = facts._requested_source(str(identifier), str(owner), str(old.id))
        assert requested_pin.source_id == str(old.id)
        selected_for_facts = []
        monkeypatch.setattr(
            facts,
            "get_media_asset_facts_batch",
            lambda sources: selected_for_facts.extend(sources) or {},
        )
        await assets_out(db, [repaired])
        assert [entry.source_id for entry in selected_for_facts] == [str(source.id)]


@pytest.mark.asyncio
async def test_original_repair_requires_matching_verified_bytes(owner, monkeypatch):
    async with AsyncSessionLocal() as db:
        asset, source = await ingest.register_verified_source(
            db,
            owner_id=owner,
            kind="image",
            object_path=f"users/{owner}/original.png",
            generation="1",
            uploaded_sha256="a" * 64,
            byte_count=12,
            content_type="image/png",
            role="original",
        )
        source.status = "deleted"
        identifier = asset.id
        await db.commit()
        monkeypatch.setattr(ingest, "ACTIVE_ASSET_LIMIT", 1)
        reservation = await ingest.reserve_library_upload(
            db, owner, {**payload("repair"), "repair_asset_id": str(identifier)}
        )
        monkeypatch.setattr(
            ingest.storage, "object_metadata", lambda path: SimpleNamespace(generation="2", size=12)
        )
        monkeypatch.setattr(
            ingest,
            "verified_source_metadata",
            lambda *args: dict(
                uploaded_sha256="b" * 64,
                generation="2",
                byte_count=12,
                content_type="image/png",
                role="original",
                metadata={},
            ),
        )
        with pytest.raises(HTTPException) as error:
            await ingest.finalize_library_upload(db, owner, reservation.id, "repair")
        assert error.value.status_code == 422


@pytest.mark.asyncio
async def test_concurrent_reservations_cannot_cross_quota(owner, monkeypatch):
    monkeypatch.setattr(ingest, "ACTIVE_ASSET_LIMIT", 1)

    async def reserve(client_id):
        async with AsyncSessionLocal() as db:
            try:
                return await ingest.reserve_library_upload(db, owner, payload(client_id))
            except HTTPException as error:
                return error.status_code

    results = await asyncio.gather(reserve("a"), reserve("b"))
    assert sum(isinstance(result, int) and result == 409 for result in results) == 1


@pytest.mark.asyncio
async def test_finalize_coalesces_full_bytes_and_dispatches_after_commit(owner, monkeypatch):
    proof = dict(
        uploaded_sha256="a" * 64,
        byte_count=12,
        content_type="image/png",
        generation="42",
        role="original",
        metadata={"width": 2, "height": 2},
    )
    monkeypatch.setattr(ingest, "verified_source_metadata", lambda *args, **kwargs: proof)
    monkeypatch.setattr(
        ingest.storage, "object_metadata", lambda path: SimpleNamespace(generation="42", size=12)
    )
    dispatched = []
    monkeypatch.setattr(ingest, "dispatch_library_analysis", lambda *args: dispatched.append(args))
    async with AsyncSessionLocal() as db:
        reservations = [
            await ingest.reserve_library_upload(db, owner, payload(key)) for key in ("a", "b")
        ]

    async def finalize(reservation):
        async with AsyncSessionLocal() as db:
            return await ingest.finalize_library_upload(db, owner, reservation.id, "event")

    results = await asyncio.gather(*(finalize(reservation) for reservation in reservations))
    assert results[0][0].id == results[1][0].id
    assert results[0][1].id == results[1][1].id
    async with AsyncSessionLocal() as db:
        assert (
            len((await db.scalars(select(MediaAsset).where(MediaAsset.user_id == owner))).all())
            == 1
        )
        retry = await ingest.finalize_library_upload(db, owner, reservations[0].id, "event")
        assert retry[0].id == results[0][0].id
    assert dispatched


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt_status", ["cleanup_pending", "deleted"])
async def test_cleanup_claim_blocks_finalization(owner, monkeypatch, receipt_status):
    from app.services.temporary_uploads import require_upload_available
    from app.tasks import mobile_upload_cleanup as cleanup

    proof = dict(
        uploaded_sha256="b" * 64,
        byte_count=12,
        content_type="image/png",
        generation="42",
        role="original",
        metadata={},
    )
    monkeypatch.setattr(ingest, "verified_source_metadata", lambda *args, **kwargs: proof)
    monkeypatch.setattr(
        ingest.storage, "object_metadata", lambda path: SimpleNamespace(generation="42", size=12)
    )
    async with AsyncSessionLocal() as db:
        reservation = await ingest.reserve_library_upload(db, owner, payload())
        reserved_path = reservation.object_path
        receipt = (
            await db.scalars(
                select(TemporaryMediaUpload).where(
                    TemporaryMediaUpload.object_path == reservation.object_path
                )
            )
        ).one()
        receipt.status = receipt_status
        await db.commit()
        with pytest.raises(HTTPException) as error:
            await ingest.finalize_library_upload(db, owner, reservation.id, "event")
        assert error.value.status_code == 409

        await db.rollback()

        # Cleanup wins first: generic legacy writers and fresh PUT requests must
        # reject the same physical path even with the library capability off.
        receipt = await db.scalar(
            select(TemporaryMediaUpload).where(TemporaryMediaUpload.object_path == reserved_path)
        )
        receipt.purpose = "library_duplicate"
        receipt.cleanup_claimed_at = datetime.now(UTC)
        receipt.retention_expires_at = datetime.now(UTC) + timedelta(days=5)
        blocked_path = receipt.object_path
        await db.commit()
        monkeypatch.setattr(ingest.settings, "media_library_enabled", False)
        with pytest.raises(HTTPException) as renewed:
            await require_upload_available(db, owner, blocked_path, renewing=True)
        assert renewed.value.status_code == 409
        await db.rollback()
        db.add(Job(user_id=owner, raw_storage_path=blocked_path))
        with pytest.raises(DBAPIError, match="temporary upload cleanup"):
            await db.flush()
        await db.rollback()
        assert await db.scalar(select(Job.id).where(Job.user_id == owner)) is None

        # Attachment wins first: the actual sweeper recognizes an independent
        # legacy binding, while a failed deletion remains claimed and fenced.
        current = datetime.now(UTC)
        bound_path = f"users/{owner}/creation-threads/bound/video.mp4"
        failed_path = f"users/{owner}/creation-threads/unused/video.mp4"
        bound = TemporaryMediaUpload(
            user_id=owner,
            object_path=bound_path,
            purpose="library_duplicate",
            retention_expires_at=current - timedelta(seconds=1),
        )
        failed = TemporaryMediaUpload(
            user_id=owner,
            object_path=failed_path,
            purpose="library_duplicate",
            retention_expires_at=current - timedelta(seconds=1),
        )
        db.add_all([bound, failed, Job(user_id=owner, raw_storage_path=bound_path)])
        await db.commit()
        deleted_paths = []
        monkeypatch.setattr(
            cleanup, "delete_object_best_effort", lambda path: deleted_paths.append(path) or False
        )
        assert await asyncio.to_thread(cleanup.cleanup_expired_temporary_uploads, now=current) == 0
        await db.refresh(bound)
        await db.refresh(failed)
        assert bound.status == "adopted" and bound.cleanup_claimed_at is None
        assert failed.status == "cleanup_pending" and failed.last_error == "storage_unavailable"
        assert deleted_paths == [failed_path]
        with pytest.raises(HTTPException) as pending:
            await require_upload_available(db, owner, failed_path)
        assert pending.value.status_code == 409
        await db.rollback()
        monkeypatch.setattr(
            cleanup, "delete_object_best_effort", lambda path: deleted_paths.append(path) or True
        )
        assert (
            await asyncio.to_thread(
                cleanup.cleanup_expired_temporary_uploads, now=current + timedelta(minutes=16)
            )
            == 1
        )
        assert deleted_paths == [failed_path, failed_path]
        await db.execute(delete(Job).where(Job.user_id == owner))
        await db.commit()


@pytest.mark.asyncio
async def test_owner_denial_archive_and_expired_reservation(owner, monkeypatch):
    async with AsyncSessionLocal() as db:
        reservation = await ingest.reserve_library_upload(db, owner, payload())
        reservation_id = reservation.id
        with pytest.raises(HTTPException) as error:
            await ingest.finalize_library_upload(db, uuid.uuid4(), reservation.id, "event")
        assert error.value.status_code == 404
        await db.rollback()
        reservation = await db.get(type(reservation), reservation_id)
        reservation.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
        with pytest.raises(HTTPException) as error:
            await ingest.finalize_library_upload(db, owner, reservation.id, "event")
        assert error.value.status_code == 409

        # Erasure wins between the rejected-attachment rollback and its fresh
        # cleanup transaction. No receipt may recreate owner data after deletion.
        await db.rollback()
        real_rollback = db.rollback
        erased = False

        async def rollback_then_erase():
            nonlocal erased
            await real_rollback()
            if not erased:
                erased = True
                async with AsyncSessionLocal() as erasure_db:
                    from app.services.job_storage_paths import lock_project_media_owner

                    await lock_project_media_owner(erasure_db, owner)
                    await erasure_db.execute(delete(User).where(User.id == owner))
                    await erasure_db.commit()

        monkeypatch.setattr(db, "rollback", rollback_then_erase)
        thread_id = uuid.uuid4()
        await ingest.preserve_rejected_uploads(
            db, owner, thread_id, [f"users/{owner}/creation-threads/{thread_id}/unused.mp4"]
        )
        assert await db.get(User, owner) is None
        assert (
            await db.scalar(
                select(TemporaryMediaUpload.id).where(TemporaryMediaUpload.user_id == owner)
            )
            is None
        )


@pytest.mark.asyncio
async def test_declared_hash_cannot_coalesce_different_proxy_bytes(owner):
    async with AsyncSessionLocal() as db:
        first, _ = await ingest.register_verified_source(
            db,
            owner_id=owner,
            kind="video",
            object_path=f"users/{owner}/media-library/analysis-proxy-a.mp4",
            generation="42",
            uploaded_sha256="a" * 64,
            byte_count=12,
            content_type="video/mp4",
            role="analysis_proxy",
            original_declared_sha256="c" * 64,
            original_byte_count=100,
        )
        second, _ = await ingest.register_verified_source(
            db,
            owner_id=owner,
            kind="video",
            object_path=f"users/{owner}/media-library/analysis-proxy-b.mp4",
            generation="43",
            uploaded_sha256="b" * 64,
            byte_count=12,
            content_type="video/mp4",
            role="analysis_proxy",
            original_declared_sha256="c" * 64,
            original_byte_count=100,
        )
        assert first.id != second.id
        first_id = first.id
        await db.commit()
        await ingest.archive_library_asset(db, owner, first.id)
        with pytest.raises(HTTPException) as error:
            await ingest.resolve_library_source(db, owner, first.id)
        assert error.value.status_code == 409
        await db.rollback()
        asset, source = await ingest.resolve_library_source(
            db, owner, first_id, allow_archived=True
        )
        assert asset.id == first_id and source.uploaded_sha256 == "a" * 64


def test_verified_bytes_reject_invalid_image_and_size(monkeypatch, tmp_path):
    monkeypatch.setattr(
        ingest.storage, "object_metadata", lambda path: SimpleNamespace(generation="42", size=12)
    )

    def download(path, local, **kwargs):
        from pathlib import Path

        Path(local).write_bytes(b"not an image")

    monkeypatch.setattr(ingest.storage, "download_generation_to_file", download)
    with pytest.raises(HTTPException) as error:
        ingest.verified_source_metadata("path", "42", "image", payload())
    assert error.value.status_code == 422


@pytest.mark.asyncio
async def test_full_500_library_rotates_only_after_verified_finalize(owner, monkeypatch):
    # Real capacity, not a reduced stand-in: reservation and invalid media cannot
    # remove any of the historical uploads before a successful verified import.
    now = datetime.now(UTC)
    ids = sorted([uuid.uuid4() for _ in range(500)])
    async with AsyncSessionLocal() as db:
        db.add_all(
            MediaAsset(
                id=identifier,
                user_id=owner,
                kind="image",
                status="ready",
                created_at=now - timedelta(days=1),
                last_used_at=now if index == 0 else None,
            )
            for index, identifier in enumerate(ids)
        )
        await db.commit()
        reservation = await ingest.reserve_library_upload(db, owner, payload())
        assert (
            len(
                (
                    await db.scalars(
                        select(MediaAsset.id).where(
                            MediaAsset.user_id == owner, MediaAsset.archived_at.is_(None)
                        )
                    )
                ).all()
            )
            == 500
        )

    def invalid(*args, **kwargs):
        raise HTTPException(422, "Invalid uploaded image")

    monkeypatch.setattr(ingest, "verified_source_metadata", invalid)
    monkeypatch.setattr(
        ingest.storage, "object_metadata", lambda path: SimpleNamespace(generation="42", size=12)
    )
    async with AsyncSessionLocal() as db:
        with pytest.raises(HTTPException, match="Invalid uploaded image"):
            await ingest.finalize_library_upload(db, owner, reservation.id, "bad")
        await db.rollback()
        assert (await db.get(MediaAsset, ids[0])).archived_at is None

    monkeypatch.setattr(
        ingest,
        "verified_source_metadata",
        lambda *args, **kwargs: dict(
            uploaded_sha256="a" * 64,
            byte_count=12,
            content_type="image/png",
            generation="42",
            role="original",
            metadata={},
        ),
    )
    async with AsyncSessionLocal() as db:
        imported, source = await ingest.finalize_library_upload(db, owner, reservation.id, "ok")
        assert (await db.get(MediaAsset, ids[0])).archived_at is not None
        active = set(
            (
                await db.scalars(
                    select(MediaAsset.id).where(
                        MediaAsset.user_id == owner, MediaAsset.archived_at.is_(None)
                    )
                )
            ).all()
        )
        assert active == set(ids[1:]) | {imported.id}
        retry, _ = await ingest.finalize_library_upload(db, owner, reservation.id, "retry")
        assert retry.id == imported.id
        # A fresh upload of the same bytes coalesces without a second eviction.
        duplicate = await ingest.reserve_library_upload(db, owner, payload("duplicate"))
        reused, same = await ingest.finalize_library_upload(db, owner, duplicate.id, "duplicate")
        assert reused.id == imported.id and same.id == source.id
        assert (
            set(
                (
                    await db.scalars(
                        select(MediaAsset.id).where(
                            MediaAsset.user_id == owner, MediaAsset.archived_at.is_(None)
                        )
                    )
                ).all()
            )
            == active
        )


@pytest.mark.asyncio
async def test_rotation_rolls_back_with_caller_transaction(owner, monkeypatch):
    monkeypatch.setattr(ingest, "ACTIVE_ASSET_LIMIT", 1)
    values = dict(
        owner_id=owner,
        kind="image",
        byte_count=12,
        content_type="image/png",
        role="original",
        generation="42",
    )
    async with AsyncSessionLocal() as db:
        old, _ = await ingest.register_verified_source(
            db, object_path=f"users/{owner}/old.png", uploaded_sha256="a" * 64, **values
        )
        old_id = old.id
        await db.commit()
        new, _ = await ingest.register_verified_source(
            db, object_path=f"users/{owner}/rejected.png", uploaded_sha256="b" * 64, **values
        )
        new_id = new.id
        await db.rollback()
        assert (await db.get(MediaAsset, old_id)).archived_at is None
        assert await db.get(MediaAsset, new_id) is None


@pytest.mark.asyncio
async def test_concurrent_unique_imports_rotate_under_owner_lock(owner, monkeypatch):
    monkeypatch.setattr(ingest, "ACTIVE_ASSET_LIMIT", 2)
    async with AsyncSessionLocal() as db:
        old = MediaAsset(
            user_id=owner,
            kind="image",
            status="ready",
            created_at=datetime.now(UTC) - timedelta(days=1),
        )
        db.add(old)
        await db.commit()
        old_id = old.id

    async def import_one(index):
        async with AsyncSessionLocal() as db:
            asset, _ = await ingest.register_verified_source(
                db,
                owner_id=owner,
                kind="image",
                byte_count=12,
                content_type="image/png",
                role="original",
                generation="42",
                uploaded_sha256=str(index) * 64,
                object_path=f"users/{owner}/{index}.png",
            )
            await db.commit()
            return asset.id

    imported = await asyncio.gather(*(import_one(index) for index in range(1, 5)))
    async with AsyncSessionLocal() as db:
        active = (
            await db.scalars(
                select(MediaAsset)
                .where(MediaAsset.user_id == owner, MediaAsset.archived_at.is_(None))
                .order_by(MediaAsset.created_at.desc(), MediaAsset.id.desc())
            )
        ).all()
        newest = (
            await db.scalars(
                select(MediaAsset.id)
                .where(MediaAsset.user_id == owner)
                .order_by(MediaAsset.created_at.desc(), MediaAsset.id.desc())
                .limit(2)
            )
        ).all()
        assert len(active) == 2 and [asset.id for asset in active] == newest
        assert set(newest) <= set(imported)
        assert (await db.get(MediaAsset, old_id)).archived_at is not None
