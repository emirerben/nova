"""PostgreSQL contract checks for KRI-384 library-source retention."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError, ProgrammingError

from app.config import settings
from app.database import sync_session
from app.models import (
    Job,
    MediaAsset,
    MediaAssetReference,
    MediaAssetSource,
    StorageRetentionEntry,
    StorageRetentionManifest,
    User,
)
from app.services import media_library_retention
from app.storage import ObjectMetadata
from app.tasks import storage_retention


@pytest.fixture
def library_source() -> tuple[MediaAssetSource, uuid.UUID]:
    """One catalogue-only source on the disposable KRI-384 database."""
    owner_id = uuid.uuid4()
    asset_id = uuid.uuid4()
    source_id = uuid.uuid4()
    now = datetime.now(UTC)
    try:
        with sync_session() as db:
            db.execute(text("SELECT 1 FROM media_asset_sources LIMIT 0"))
            db.add(User(id=owner_id, email=f"library-retention-{owner_id}@test.local"))
            db.flush()
            db.add(
                MediaAsset(
                    id=asset_id,
                    user_id=owner_id,
                    kind="video",
                    status="ready",
                    created_at=now - timedelta(days=100),
                    updated_at=now - timedelta(days=100),
                )
            )
            source = MediaAssetSource(
                id=source_id,
                user_id=owner_id,
                asset_id=asset_id,
                role="original",
                object_path=f"users/{owner_id}/library/{source_id}.mp4",
                object_generation="42",
                byte_count=123,
                content_type="video/mp4",
                created_at=now - timedelta(days=90),
                last_used_at=now - timedelta(days=90),
            )
            db.add(source)
            db.commit()
    except (OperationalError, ProgrammingError) as exc:
        pytest.skip(f"KRI-384 retention database is not migrated: {exc!r}")

    try:
        yield source, asset_id
    finally:
        with sync_session() as db:
            db.execute(
                text("DELETE FROM media_asset_references WHERE user_id = :owner"),
                {"owner": owner_id},
            )
            db.execute(
                text("DELETE FROM storage_retention_entries WHERE creator_id = :owner"),
                {"owner": owner_id},
            )
            db.execute(text("DELETE FROM jobs WHERE user_id = :owner"), {"owner": owner_id})
            db.execute(text("DELETE FROM media_assets WHERE user_id = :owner"), {"owner": owner_id})
            db.execute(text("DELETE FROM users WHERE id = :owner"), {"owner": owner_id})
            db.commit()


def test_catalogue_source_warns_then_deletes_but_a_shared_ref_or_recent_use_pins_it(
    library_source: tuple[MediaAssetSource, uuid.UUID],
) -> None:
    source, asset_id = library_source
    now = datetime.now(UTC)
    with sync_session() as db:
        candidates, _, _ = media_library_retention.source_candidates(db, now=now, limit=10)
        ours = [candidate for candidate in candidates if candidate.source_id == source.id]
        assert [(candidate.action, candidate.reason) for candidate in ours] == [
            ("warn", "inactive_library_source_warning")
        ]
        # Late first warnings must advertise the actual earliest deletion,
        # rather than a day-90 deadline that has already arrived.
        assert ours[0].eligible_at >= now + timedelta(days=7)

        manifest = StorageRetentionManifest(
            status="completed",
            generated_at=now - timedelta(days=7),
            report_only_until=now - timedelta(days=7),
            summary_json={},
        )
        db.add(manifest)
        db.flush()
        db.add(
            StorageRetentionEntry(
                manifest_id=manifest.id,
                creator_id=source.user_id,
                object_path=source.object_path,
                object_generation=source.object_generation,
                action="warn",
                reason="inactive_library_source_warning",
                eligible_at=now - timedelta(days=7),
                size_bytes=source.byte_count,
                created_at=now - timedelta(days=7),
            )
        )
        db.commit()

    with sync_session() as db:
        candidates, _, _ = media_library_retention.source_candidates(db, now=now, limit=10)
        ours = [candidate for candidate in candidates if candidate.source_id == source.id]
        assert [(candidate.action, candidate.reason) for candidate in ours] == [
            ("delete", "inactive_library_source")
        ]

        job_id = uuid.uuid4()
        job = Job(
            id=job_id,
            user_id=source.user_id,
            status="completed",
            job_type="template",
            raw_storage_path=f"users/{source.user_id}/jobs/{job_id}/source.mp4",
        )
        db.add(job)
        db.flush()
        db.add(
            MediaAssetReference(
                user_id=source.user_id,
                asset_id=asset_id,
                source_id=None,
                job_id=job.id,
            )
        )
        db.commit()

    with sync_session() as db:
        candidates, _, _ = media_library_retention.source_candidates(db, now=now, limit=10)
        assert not [candidate for candidate in candidates if candidate.source_id == source.id]
        db.execute(
            text("DELETE FROM media_asset_references WHERE asset_id = :asset"), {"asset": asset_id}
        )
        source_row = db.get(MediaAssetSource, source.id)
        source_row.last_used_at = now
        db.commit()

    with sync_session() as db:
        candidates, _, _ = media_library_retention.source_candidates(db, now=now, limit=10)
        assert not [candidate for candidate in candidates if candidate.source_id == source.id]


def test_legacy_job_scan_leaves_an_adopted_generation_for_library_retention(
    library_source: tuple[MediaAssetSource, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A legacy job prefix can find an adopted source before its library page.

    The source has been unused for 100 days, so library retention must create
    its day-83 warning.  Historically the legacy scan classified this exact
    generation as a superseded derivative and `seen` suppressed the library
    candidate, allowing a later deletion without the creator warning.
    """
    source, _ = library_source
    now = datetime.now(UTC)
    job_id = uuid.uuid4()
    path = f"users/{source.user_id}/jobs/{job_id}/render.mp4"
    overlap_asset_id = uuid.uuid4()
    overlap_source_id = uuid.uuid4()
    overlap_source = MediaAssetSource(
        id=overlap_source_id,
        user_id=source.user_id,
        asset_id=overlap_asset_id,
        role="original",
        object_path=path,
        object_generation="legacy-overlap",
        byte_count=456,
        content_type="video/mp4",
        created_at=now - timedelta(days=100),
        last_used_at=now - timedelta(days=100),
    )
    with sync_session() as db:
        db.add(
            MediaAsset(
                id=overlap_asset_id,
                user_id=source.user_id,
                kind="video",
                status="ready",
                created_at=now - timedelta(days=100),
                updated_at=now - timedelta(days=100),
            )
        )
        db.add(overlap_source)
        db.add(
            Job(
                id=job_id,
                user_id=source.user_id,
                status="completed",
                job_type="template",
                raw_storage_path=f"users/{source.user_id}/jobs/{job_id}/source.mp4",
                created_at=now - timedelta(days=100),
                updated_at=now - timedelta(days=100),
            )
        )
        db.commit()

    metadata = ObjectMetadata(
        path=path,
        generation=overlap_source.object_generation,
        etag=None,
        size=overlap_source.byte_count,
        content_type="video/mp4",
        created_at=now - timedelta(days=100),
    )
    monkeypatch.setattr(
        storage_retention.storage,
        "list_object_metadata",
        lambda prefix: [metadata] if prefix == f"users/{source.user_id}/jobs/{job_id}/" else [],
    )
    monkeypatch.setattr(settings, "storage_retention_enabled", True)
    monkeypatch.setattr(settings, "storage_retention_scan_jobs", 10_000)
    monkeypatch.setattr(storage_retention, "_previous_library_scan_cursor", lambda _db: None)

    manifest_id = storage_retention.build_retention_manifest(now=now)
    assert manifest_id is not None
    with sync_session() as db:
        entries = db.scalars(
            select(StorageRetentionEntry).where(
                StorageRetentionEntry.manifest_id == uuid.UUID(manifest_id),
                StorageRetentionEntry.creator_id == source.user_id,
                StorageRetentionEntry.object_path == path,
            )
        ).all()
        assert [(entry.action, entry.reason) for entry in entries] == [
            ("warn", "inactive_library_source_warning")
        ]
        db.get(StorageRetentionManifest, uuid.UUID(manifest_id)).status = "completed"
        db.commit()


def _approved_library_entry(
    source: MediaAssetSource,
    *,
    action: str,
    reason: str,
    warning_created_at: datetime | None = None,
) -> uuid.UUID:
    now = datetime.now(UTC)
    with sync_session() as db:
        manifest = StorageRetentionManifest(
            status="approved",
            generated_at=now - timedelta(days=8),
            report_only_until=now - timedelta(days=1),
            approved_at=now - timedelta(hours=1),
            approved_by="pytest",
            summary_json={},
        )
        db.add(manifest)
        db.flush()
        if warning_created_at is not None:
            warning_manifest = StorageRetentionManifest(
                status="completed",
                generated_at=warning_created_at,
                report_only_until=warning_created_at,
                summary_json={},
            )
            db.add(warning_manifest)
            db.flush()
            db.add(
                StorageRetentionEntry(
                    manifest_id=warning_manifest.id,
                    creator_id=source.user_id,
                    object_path=source.object_path,
                    object_generation=source.object_generation,
                    action="warn",
                    reason="inactive_library_source_warning",
                    eligible_at=warning_created_at,
                    size_bytes=source.byte_count,
                    created_at=warning_created_at,
                )
            )
        entry = StorageRetentionEntry(
            manifest_id=manifest.id,
            creator_id=source.user_id,
            object_path=source.object_path,
            object_generation=source.object_generation,
            action=action,
            reason=reason,
            eligible_at=now - timedelta(days=1),
            size_bytes=source.byte_count,
        )
        db.add(entry)
        db.commit()
        return manifest.id


@pytest.mark.parametrize("delete_reason", ("inactive_library_source", "superseded_derivative"))
def test_warning_and_late_first_warning_never_fence_or_delete_library_source(
    library_source: tuple[MediaAssetSource, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
    delete_reason: str,
) -> None:
    source, _ = library_source
    monkeypatch.setattr(settings, "storage_retention_delete_enabled", True)
    monkeypatch.setattr(
        storage_retention.storage,
        "object_metadata_once",
        lambda *_args, **_kwargs: SimpleNamespace(generation=source.object_generation),
    )
    monkeypatch.setattr(storage_retention.storage, "delete_object_generation", pytest.fail)
    warning_manifest = _approved_library_entry(
        source, action="warn", reason="inactive_library_source_warning"
    )
    assert storage_retention.execute_retention_manifest(str(warning_manifest)) == {
        "deleted": 0,
        "skipped": 0,
        "failed": 0,
    }
    with sync_session() as db:
        assert db.get(MediaAssetSource, source.id).status == "available"

    late_manifest = _approved_library_entry(
        source,
        action="delete",
        reason=delete_reason,
        warning_created_at=datetime.now(UTC),
    )
    assert storage_retention.execute_retention_manifest(str(late_manifest)) == {
        "deleted": 0,
        "skipped": 1,
        "failed": 0,
    }
    with sync_session() as db:
        assert db.get(MediaAssetSource, source.id).status == "available"


@pytest.mark.parametrize("delete_reason", ("inactive_library_source", "superseded_derivative"))
def test_library_source_deletes_only_after_a_durable_seven_day_warning(
    library_source: tuple[MediaAssetSource, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
    delete_reason: str,
) -> None:
    source, _ = library_source
    monkeypatch.setattr(settings, "storage_retention_delete_enabled", True)
    monkeypatch.setattr(
        storage_retention.storage,
        "object_metadata_once",
        lambda *_args, **_kwargs: SimpleNamespace(generation=source.object_generation),
    )
    delete = MagicMock()
    monkeypatch.setattr(storage_retention.storage, "delete_object_generation", delete)
    with sync_session() as db:
        db.get(MediaAssetSource, source.id).last_used_at = datetime.now(UTC) - timedelta(days=100)
        db.commit()
    manifest_id = _approved_library_entry(
        source,
        action="delete",
        reason=delete_reason,
        # It is both seven days old and after this source's day-83 threshold.
        warning_created_at=datetime.now(UTC) - timedelta(days=8),
    )

    assert storage_retention.execute_retention_manifest(str(manifest_id)) == {
        "deleted": 1,
        "skipped": 0,
        "failed": 0,
    }
    delete.assert_called_once_with(
        source.object_path,
        generation=source.object_generation,
        timeout_s=3.0,
    )
    with sync_session() as db:
        assert db.get(MediaAssetSource, source.id).status == "deleted"


@pytest.mark.parametrize("delete_reason", ("inactive_library_source", "superseded_derivative"))
def test_reuse_invalidates_an_old_warning_until_the_current_window_is_warned(
    library_source: tuple[MediaAssetSource, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
    delete_reason: str,
) -> None:
    source, _ = library_source
    now = datetime.now(UTC)
    # The source was warned during its first inactive window, then used again.
    # At the next day 90 it must issue a new warning rather than deleting.
    with sync_session() as db:
        source_row = db.get(MediaAssetSource, source.id)
        source_row.created_at = now - timedelta(days=200)
        source_row.last_used_at = now - timedelta(days=90)
        db.commit()

    stale_warning_at = now - timedelta(days=100)
    manifest_id = _approved_library_entry(
        source,
        action="delete",
        reason="superseded_derivative",
        warning_created_at=stale_warning_at,
    )
    with sync_session() as db:
        candidates, _, _ = media_library_retention.source_candidates(db, now=now, limit=10)
    ours = [candidate for candidate in candidates if candidate.source_id == source.id]
    assert [(candidate.action, candidate.reason) for candidate in ours] == [
        ("warn", "inactive_library_source_warning")
    ]

    monkeypatch.setattr(settings, "storage_retention_delete_enabled", True)
    monkeypatch.setattr(
        storage_retention.storage,
        "object_metadata_once",
        lambda *_args, **_kwargs: SimpleNamespace(generation=source.object_generation),
    )
    monkeypatch.setattr(storage_retention.storage, "delete_object_generation", pytest.fail)
    assert storage_retention.execute_retention_manifest(str(manifest_id)) == {
        "deleted": 0,
        "skipped": 1,
        "failed": 0,
    }
    with sync_session() as db:
        assert db.get(MediaAssetSource, source.id).status == "available"
