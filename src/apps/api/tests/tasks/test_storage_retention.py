from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, OperationalError, ProgrammingError

from app.config import settings
from app.database import sync_engine, sync_session
from app.models import (
    ContentPlan,
    Job,
    Persona,
    PlanItem,
    PlanItemAsset,
    StorageRetentionEntry,
    StorageRetentionManifest,
    User,
)
from app.services.job_storage_paths import project_media_reference_lock_key
from app.storage import ObjectMetadata
from app.tasks import storage_retention
from app.tasks.storage_retention import _candidate

NOW = datetime(2026, 9, 8, tzinfo=UTC)


def _job(*, age_days: int, active: bool = False):
    job_id = uuid.uuid4()
    updated_at = NOW - timedelta(days=age_days)
    return (
        SimpleNamespace(
            id=job_id,
            created_at=updated_at,
            updated_at=updated_at,
        ),
        ({job_id} if active else set()),
    )


def _metadata(path: str, *, age_days: int) -> ObjectMetadata:
    return ObjectMetadata(
        path=path,
        generation="42",
        etag=None,
        size=123,
        content_type="video/mp4",
        created_at=NOW - timedelta(days=age_days),
    )


def test_active_or_database_protected_objects_never_enter_manifest() -> None:
    job, active_jobs = _job(age_days=500, active=True)
    metadata = _metadata(f"generative-jobs/{job.id}/old.mp4", age_days=500)
    assert (
        _candidate(
            metadata=metadata,
            job=job,
            now=NOW,
            protected_paths=set(),
            active_jobs=active_jobs,
            source_paths=set(),
            current_paths=set(),
        )
        is None
    )

    job, _ = _job(age_days=500)
    assert (
        _candidate(
            metadata=metadata,
            job=job,
            now=NOW,
            protected_paths={metadata.path},
            active_jobs=set(),
            source_paths=set(),
            current_paths=set(),
        )
        is None
    )


def test_historical_authenticated_raw_source_is_discovered_and_listed(monkeypatch) -> None:
    creator_id = uuid.uuid4()
    job_id = uuid.uuid4()
    path = f"{creator_id}/{job_id}/raw.mp4"
    job = SimpleNamespace(
        id=job_id,
        user_id=creator_id,
        raw_storage_path=path,
        all_candidates={},
        assembly_plan={},
    )
    db = MagicMock()
    db.scalars.return_value = []

    source_paths, current_paths = storage_retention._job_references(db, job)

    assert source_paths == {path}
    assert current_paths == {path}
    listed_prefixes: list[str] = []
    monkeypatch.setattr(
        storage_retention.storage,
        "list_object_metadata",
        lambda prefix: listed_prefixes.append(prefix) or [],
    )
    monkeypatch.setattr(
        storage_retention.storage,
        "object_metadata_once",
        lambda candidate, *, timeout_s: _metadata(candidate, age_days=90),
    )

    metadata, errors = storage_retention._metadata_for_job(job, source_paths)

    assert errors == 0
    assert f"{creator_id}/{job_id}/" in listed_prefixes
    assert [row.path for row in metadata] == [path]


def test_reference_writer_trigger_shares_the_owner_retention_lock(approved_manifest) -> None:
    creator_id = uuid.uuid4()
    job_id = uuid.uuid4()
    key = project_media_reference_lock_key(creator_id)
    with sync_session() as db:
        db.add(User(id=creator_id, email=f"retention-lock-{creator_id}@test.local"))
        db.commit()
        assert db.scalar(select(func.project_media_reference_lock_key_0103(creator_id))) == key

    try:
        with sync_engine.connect() as holder:
            holder_tx = holder.begin()
            holder.execute(select(func.pg_advisory_xact_lock(key)))
            with sync_engine.connect() as writer:
                writer_tx = writer.begin()
                writer.execute(text("SET LOCAL lock_timeout = '100ms'"))
                with pytest.raises(DBAPIError, match="lock timeout"):
                    writer.execute(
                        text(
                            "INSERT INTO jobs (id, user_id, status, job_type, raw_storage_path) "
                            "VALUES (:id, :user_id, 'queued', 'template', :path)"
                        ),
                        {
                            "id": job_id,
                            "user_id": creator_id,
                            "path": f"users/{creator_id}/locked.mp4",
                        },
                    )
                writer_tx.rollback()
            holder_tx.rollback()
    finally:
        with sync_session() as db:
            db.execute(text("DELETE FROM jobs WHERE id = :id"), {"id": job_id})
            db.execute(text("DELETE FROM users WHERE id = :id"), {"id": creator_id})
            db.commit()


def test_reference_writer_rejects_path_deleted_after_its_transaction_began(
    approved_manifest,
) -> None:
    creator_id = uuid.uuid4()
    job_id = uuid.uuid4()
    path = f"users/{creator_id}/validated-before-delete.mp4"
    manifest_id, entry_id, _ = approved_manifest(creator_id=creator_id, path=path)
    with sync_session() as db:
        db.add(User(id=creator_id, email=f"retention-race-{creator_id}@test.local"))
        db.commit()

    try:
        with sync_engine.connect() as writer:
            writer_tx = writer.begin()
            # This stands in for the request's DB lookup before remote GCS
            # metadata validation and fixes its transaction start timestamp.
            writer.execute(text("SELECT transaction_timestamp()"))
            with sync_engine.begin() as deleter:
                deleter.execute(
                    select(func.pg_advisory_xact_lock(project_media_reference_lock_key(creator_id)))
                )
                deleter.execute(
                    text(
                        "UPDATE storage_retention_entries "
                        "SET status = 'deleted', deleted_at = clock_timestamp() "
                        "WHERE id = :entry_id"
                    ),
                    {"entry_id": entry_id},
                )

            with pytest.raises(DBAPIError, match="raced with retention deletion"):
                writer.execute(
                    text(
                        "INSERT INTO jobs (id, user_id, status, job_type, raw_storage_path) "
                        "VALUES (:id, :user_id, 'queued', 'template', :path)"
                    ),
                    {"id": job_id, "user_id": creator_id, "path": path},
                )
            writer_tx.rollback()
    finally:
        with sync_session() as db:
            db.execute(text("DELETE FROM jobs WHERE id = :id"), {"id": job_id})
            db.execute(text("DELETE FROM users WHERE id = :id"), {"id": creator_id})
            db.commit()

        # Keep the fixture manifest cleanup authoritative; the local name is
        # retained to make the tombstone relationship explicit in this test.
        assert manifest_id is not None


def test_current_job_reference_rejects_a_tombstoned_job_path(approved_manifest) -> None:
    creator_id = uuid.uuid4()
    persona_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    item_id = uuid.uuid4()
    job_id = uuid.uuid4()
    path = f"users/{creator_id}/jobs/{job_id}/output.mp4"
    with sync_session() as db:
        db.add(User(id=creator_id, email=f"retention-current-{creator_id}@test.local"))
        db.flush()
        db.add(Persona(id=persona_id, user_id=creator_id, persona_status="ready"))
        db.add(
            Job(
                id=job_id,
                user_id=creator_id,
                status="done",
                job_type="template",
                raw_storage_path=path,
                assembly_plan={"output_path": path},
            )
        )
        db.flush()
        db.add(ContentPlan(id=plan_id, user_id=creator_id, persona_id=persona_id))
        db.flush()
        db.add(PlanItem(id=item_id, content_plan_id=plan_id, position=0, idea="test"))
        db.commit()
    manifest_id, entry_id, _ = approved_manifest(
        creator_id=creator_id,
        job_id=job_id,
        path=path,
    )

    try:
        with sync_session() as db:
            entry = db.get(StorageRetentionEntry, entry_id)
            entry.status = "deleting"
            entry.deleted_at = datetime.now(UTC)
            db.commit()
        with sync_engine.begin() as writer:
            with pytest.raises(DBAPIError, match="raced with retention deletion"):
                writer.execute(
                    text("UPDATE plan_items SET current_job_id = :job_id WHERE id = :item_id"),
                    {"job_id": job_id, "item_id": item_id},
                )
    finally:
        with sync_session() as db:
            db.execute(text("DELETE FROM plan_items WHERE id = :id"), {"id": item_id})
            db.execute(text("DELETE FROM content_plans WHERE id = :id"), {"id": plan_id})
            db.execute(text("DELETE FROM jobs WHERE id = :id"), {"id": job_id})
            db.execute(text("DELETE FROM personas WHERE id = :id"), {"id": persona_id})
            db.execute(text("DELETE FROM users WHERE id = :id"), {"id": creator_id})
            db.commit()
        assert manifest_id is not None


def test_plan_item_asset_writer_rejects_a_tombstoned_path(approved_manifest) -> None:
    creator_id = uuid.uuid4()
    persona_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    item_id = uuid.uuid4()
    asset_id = uuid.uuid4()
    path = f"users/{creator_id}/plan/{item_id}/pool/source.mp4"
    with sync_session() as db:
        db.add(User(id=creator_id, email=f"retention-asset-race-{creator_id}@test.local"))
        db.flush()
        db.add(Persona(id=persona_id, user_id=creator_id, persona_status="ready"))
        db.flush()
        db.add(ContentPlan(id=plan_id, user_id=creator_id, persona_id=persona_id))
        db.flush()
        db.add(PlanItem(id=item_id, content_plan_id=plan_id, position=0, idea="test"))
        db.commit()
    manifest_id, entry_id, _ = approved_manifest(creator_id=creator_id, path=path)

    try:
        with sync_session() as db:
            entry = db.get(StorageRetentionEntry, entry_id)
            entry.status = "deleting"
            entry.deleted_at = datetime.now(UTC)
            db.commit()
        with sync_engine.begin() as writer:
            with pytest.raises(DBAPIError, match="raced with retention deletion"):
                writer.execute(
                    text(
                        "INSERT INTO plan_item_assets "
                        "(id, plan_item_id, user_id, gcs_path, kind, status) "
                        "VALUES (:id, :item_id, :user_id, :path, 'video', 'ready')"
                    ),
                    {
                        "id": asset_id,
                        "item_id": item_id,
                        "user_id": creator_id,
                        "path": path,
                    },
                )
    finally:
        with sync_session() as db:
            db.execute(text("DELETE FROM plan_item_assets WHERE id = :id"), {"id": asset_id})
            db.execute(text("DELETE FROM plan_items WHERE id = :id"), {"id": item_id})
            db.execute(text("DELETE FROM content_plans WHERE id = :id"), {"id": plan_id})
            db.execute(text("DELETE FROM personas WHERE id = :id"), {"id": persona_id})
            db.execute(text("DELETE FROM users WHERE id = :id"), {"id": creator_id})
            db.commit()
        assert manifest_id is not None


def test_inactive_source_warns_at_83_days_and_deletes_at_90() -> None:
    job, active_jobs = _job(age_days=83)
    path = f"users/creator/jobs/{job.id}/sources/input.mp4"
    warning = _candidate(
        metadata=_metadata(path, age_days=83),
        job=job,
        now=NOW,
        protected_paths=set(),
        active_jobs=active_jobs,
        source_paths={path},
        current_paths={path},
    )
    assert warning is not None
    assert warning[:2] == ("warn", "inactive_source_warning")

    job, active_jobs = _job(age_days=90)
    deletion = _candidate(
        metadata=_metadata(path, age_days=90),
        job=job,
        now=NOW,
        protected_paths=set(),
        active_jobs=active_jobs,
        source_paths={path},
        current_paths={path},
        warned_at=NOW - timedelta(days=7),
    )
    assert deletion is not None
    assert deletion[:2] == ("delete", "inactive_source_or_editable")

    first_discovered_at_delete_age = _candidate(
        metadata=_metadata(path, age_days=90),
        job=job,
        now=NOW,
        protected_paths=set(),
        active_jobs=active_jobs,
        source_paths={path},
        current_paths={path},
    )
    assert first_discovered_at_delete_age is not None
    assert first_discovered_at_delete_age[:2] == ("warn", "inactive_source_warning")


def test_latest_final_and_poster_are_retained_for_365_days() -> None:
    job, active_jobs = _job(age_days=364)
    for path in (
        f"generative-jobs/{job.id}/final.mp4",
        f"job-posters/{job.id}/final.jpg",
    ):
        assert (
            _candidate(
                metadata=_metadata(path, age_days=364),
                job=job,
                now=NOW,
                protected_paths=set(),
                active_jobs=active_jobs,
                source_paths=set(),
                current_paths={path},
            )
            is None
        )

    job, active_jobs = _job(age_days=365)
    poster = f"job-posters/{job.id}/final.jpg"
    candidate = _candidate(
        metadata=_metadata(poster, age_days=365),
        job=job,
        now=NOW,
        protected_paths=set(),
        active_jobs=active_jobs,
        source_paths=set(),
        current_paths={poster},
    )
    assert candidate is not None
    assert candidate[:2] == ("delete", "poster")


def test_superseded_derivative_is_candidate_after_seven_days() -> None:
    job, active_jobs = _job(age_days=7)
    metadata = _metadata(f"generative-jobs/{job.id}/superseded.mp4", age_days=7)
    candidate = _candidate(
        metadata=metadata,
        job=job,
        now=NOW,
        protected_paths=set(),
        active_jobs=active_jobs,
        source_paths=set(),
        current_paths=set(),
    )
    assert candidate is not None
    assert candidate[:2] == ("delete", "superseded_derivative")


@pytest.fixture
def approved_manifest(monkeypatch: pytest.MonkeyPatch):
    db_name = make_url(settings.database_url).database or ""
    if not db_name.endswith("_test"):
        pytest.skip(f"refusing to write to non-test database {db_name!r}")
    try:
        with sync_session() as probe:
            probe.execute(text("SELECT 1 FROM storage_retention_manifests LIMIT 0"))
    except OperationalError as exc:
        pytest.skip(f"Postgres not reachable for retention integration test: {exc!r}")
    except ProgrammingError as exc:
        pytest.fail(f"nova_test was not migrated to the retention schema: {exc!r}")

    manifest_ids: list[uuid.UUID] = []
    monkeypatch.setattr(settings, "storage_retention_delete_enabled", True)
    monkeypatch.setattr(storage_retention, "_protected_references", lambda _db: (set(), set()))

    def create(
        *,
        generation: str = "42",
        creator_id: uuid.UUID | None = None,
        job_id: uuid.UUID | None = None,
        path: str | None = None,
        reason: str = "superseded_derivative",
    ) -> tuple[uuid.UUID, uuid.UUID, str]:
        manifest_id = uuid.uuid4()
        entry_id = uuid.uuid4()
        path = path or f"generative-jobs/retention-test/{manifest_id}.mp4"
        with sync_session() as db:
            manifest = StorageRetentionManifest(
                id=manifest_id,
                status="approved",
                generated_at=NOW - timedelta(days=8),
                report_only_until=NOW - timedelta(days=1),
                approved_at=NOW - timedelta(hours=1),
                approved_by="pytest",
                summary_json={},
            )
            db.add(manifest)
            db.flush()
            db.add(
                StorageRetentionEntry(
                    id=entry_id,
                    manifest_id=manifest_id,
                    creator_id=creator_id or uuid.uuid4(),
                    job_id=job_id,
                    object_path=path,
                    object_generation=generation,
                    action="delete",
                    reason=reason,
                    eligible_at=NOW - timedelta(days=1),
                    size_bytes=123,
                    status="pending",
                )
            )
            db.commit()
        manifest_ids.append(manifest_id)
        return manifest_id, entry_id, path

    yield create

    with sync_session() as db:
        db.execute(
            text("DELETE FROM storage_retention_entries WHERE manifest_id = ANY(:ids)"),
            {"ids": manifest_ids},
        )
        db.execute(
            text("DELETE FROM storage_retention_manifests WHERE id = ANY(:ids)"),
            {"ids": manifest_ids},
        )
        db.commit()


def test_executor_commits_a_reclaimable_lease_before_remote_deletes(
    approved_manifest, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_id, entry_id, path = approved_manifest()
    durable_states: list[tuple[str, str, bool]] = []
    monkeypatch.setattr(
        storage_retention.storage,
        "object_metadata_once",
        lambda *_args, **_kwargs: _metadata(path, age_days=30),
    )

    def delete_once(*_args, **_kwargs) -> None:
        with sync_session() as observer:
            durable = observer.get(StorageRetentionManifest, manifest_id)
            durable_entry = observer.get(StorageRetentionEntry, entry_id)
            durable_states.append(
                (durable.status, durable_entry.status, durable_entry.deleted_at is not None)
            )
            assert durable.execution_lease_id is not None
            assert durable.execution_lease_expires_at is not None

    monkeypatch.setattr(storage_retention.storage, "delete_object_generation", delete_once)

    result = storage_retention.execute_retention_manifest(str(manifest_id))

    assert result == {"deleted": 1, "skipped": 0, "failed": 0}
    assert durable_states == [("executing", "deleting", True)]
    with sync_session() as db:
        assert db.get(StorageRetentionManifest, manifest_id).status == "completed"
        entry = db.get(StorageRetentionEntry, entry_id)
        assert entry.status == "deleted"
        assert entry.deleted_at is not None


def test_executor_crash_rolls_manifest_back_to_retryable_approved_state(
    approved_manifest, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_id, entry_id, path = approved_manifest()
    monkeypatch.setattr(
        storage_retention.storage,
        "object_metadata_once",
        lambda *_args, **_kwargs: _metadata(path, age_days=30),
    )

    def crash(*_args, **_kwargs) -> None:
        raise KeyboardInterrupt("simulated worker loss")

    monkeypatch.setattr(storage_retention.storage, "delete_object_generation", crash)

    with pytest.raises(KeyboardInterrupt, match="worker loss"):
        storage_retention.execute_retention_manifest(str(manifest_id))

    with sync_session() as db:
        assert db.get(StorageRetentionManifest, manifest_id).status == "approved"
        assert db.get(StorageRetentionEntry, entry_id).status == "deleting"

    monkeypatch.setattr(
        storage_retention.storage,
        "object_metadata_once",
        MagicMock(side_effect=FileNotFoundError),
    )
    result = storage_retention.execute_retention_manifest(str(manifest_id))

    assert result == {"deleted": 1, "skipped": 0, "failed": 0}
    with sync_session() as db:
        assert db.get(StorageRetentionManifest, manifest_id).status == "completed"
        assert db.get(StorageRetentionEntry, entry_id).status == "deleted"


def test_delayed_retry_refreshes_deleted_tombstone_when_absence_is_confirmed(
    approved_manifest, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_id, entry_id, _path = approved_manifest()
    stale_tombstone = datetime.now(UTC) - timedelta(hours=2)
    with sync_session() as db:
        entry = db.get(StorageRetentionEntry, entry_id)
        entry.status = "deleting"
        entry.deleted_at = stale_tombstone
        db.commit()
    monkeypatch.setattr(
        storage_retention.storage,
        "object_metadata_once",
        MagicMock(side_effect=FileNotFoundError),
    )
    started_at = datetime.now(UTC)

    result = storage_retention.execute_retention_manifest(str(manifest_id))

    assert result == {"deleted": 1, "skipped": 0, "failed": 0}
    with sync_session() as db:
        entry = db.get(StorageRetentionEntry, entry_id)
        assert entry.status == "deleted"
        assert entry.deleted_at >= started_at


def test_executor_keeps_tombstone_when_delete_outcome_is_unknown(
    approved_manifest, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_id, entry_id, path = approved_manifest()
    monkeypatch.setattr(
        storage_retention.storage,
        "object_metadata_once",
        lambda *_args, **_kwargs: _metadata(path, age_days=30),
    )
    monkeypatch.setattr(
        storage_retention.storage,
        "delete_object_generation",
        MagicMock(side_effect=TimeoutError("unknown provider outcome")),
    )

    result = storage_retention.execute_retention_manifest(str(manifest_id))

    assert result == {"deleted": 0, "skipped": 0, "failed": 1}
    with sync_session() as db:
        assert db.get(StorageRetentionManifest, manifest_id).status == "approved"
        entry = db.get(StorageRetentionEntry, entry_id)
        assert entry.status == "deleting"
        assert entry.deleted_at is not None
        assert entry.error_detail == "TimeoutError"


def test_executor_does_not_steal_an_unexpired_execution_lease(
    approved_manifest, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_id, entry_id, _path = approved_manifest()
    with sync_session() as db:
        manifest = db.get(StorageRetentionManifest, manifest_id)
        manifest.status = "executing"
        manifest.execution_lease_id = uuid.uuid4()
        manifest.execution_lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        db.commit()
    delete = MagicMock()
    monkeypatch.setattr(storage_retention.storage, "delete_object_generation", delete)

    result = storage_retention.execute_retention_manifest(str(manifest_id))

    assert result == {"deleted": 0, "skipped": 0, "failed": 0}
    delete.assert_not_called()
    with sync_session() as db:
        assert db.get(StorageRetentionManifest, manifest_id).status == "executing"
        assert db.get(StorageRetentionEntry, entry_id).status == "pending"


def test_executor_reclaims_an_expired_execution_lease(
    approved_manifest, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_id, entry_id, path = approved_manifest()
    with sync_session() as db:
        manifest = db.get(StorageRetentionManifest, manifest_id)
        manifest.status = "executing"
        manifest.execution_lease_id = uuid.uuid4()
        manifest.execution_lease_expires_at = NOW - timedelta(minutes=1)
        db.commit()
    monkeypatch.setattr(
        storage_retention.storage,
        "object_metadata_once",
        lambda *_args, **_kwargs: _metadata(path, age_days=30),
    )
    monkeypatch.setattr(storage_retention.storage, "delete_object_generation", MagicMock())

    result = storage_retention.execute_retention_manifest(str(manifest_id))

    assert result == {"deleted": 1, "skipped": 0, "failed": 0}
    with sync_session() as db:
        assert db.get(StorageRetentionManifest, manifest_id).status == "completed"
        assert db.get(StorageRetentionEntry, entry_id).status == "deleted"


def test_executor_skips_a_changed_object_generation(
    approved_manifest, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_id, entry_id, path = approved_manifest(generation="old")
    changed = _metadata(path, age_days=30)
    changed = ObjectMetadata(
        path=changed.path,
        generation="new",
        etag=changed.etag,
        size=changed.size,
        content_type=changed.content_type,
        created_at=changed.created_at,
    )
    delete = MagicMock()
    monkeypatch.setattr(
        storage_retention.storage,
        "object_metadata_once",
        lambda *_args, **_kwargs: changed,
    )
    monkeypatch.setattr(storage_retention.storage, "delete_object_generation", delete)

    result = storage_retention.execute_retention_manifest(str(manifest_id))

    assert result == {"deleted": 0, "skipped": 1, "failed": 0}
    delete.assert_not_called()
    with sync_session() as db:
        assert db.get(StorageRetentionManifest, manifest_id).status == "completed"
        assert db.get(StorageRetentionEntry, entry_id).status == "skipped"


def test_executor_requires_a_durable_seven_day_source_warning(
    approved_manifest, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_id, entry_id, path = approved_manifest(reason="inactive_source_or_editable")
    delete = MagicMock()
    monkeypatch.setattr(
        storage_retention.storage,
        "object_metadata_once",
        lambda *_args, **_kwargs: _metadata(path, age_days=90),
    )
    monkeypatch.setattr(storage_retention.storage, "delete_object_generation", delete)

    result = storage_retention.execute_retention_manifest(str(manifest_id))

    assert result == {"deleted": 0, "skipped": 1, "failed": 0}
    delete.assert_not_called()
    with sync_session() as db:
        entry = db.get(StorageRetentionEntry, entry_id)
        assert entry.status == "skipped"
        assert entry.error_detail == "creator_warning_lead_time_missing"


def test_executor_protects_a_source_reused_by_a_newer_inactive_job(
    approved_manifest, monkeypatch: pytest.MonkeyPatch
) -> None:
    creator_id = uuid.uuid4()
    old_job_id = uuid.uuid4()
    newer_job_id = uuid.uuid4()
    path = f"users/{creator_id}/jobs/{old_job_id}/sources/shared.mp4"
    with sync_session() as db:
        db.add(User(id=creator_id, email=f"retention-{creator_id}@test.local"))
        db.add_all(
            [
                Job(
                    id=old_job_id,
                    user_id=creator_id,
                    status="done",
                    job_type="template",
                    raw_storage_path=path,
                    assembly_plan={},
                    created_at=NOW - timedelta(days=100),
                    updated_at=NOW - timedelta(days=100),
                ),
                Job(
                    id=newer_job_id,
                    user_id=creator_id,
                    status="done",
                    job_type="template",
                    raw_storage_path=f"users/{creator_id}/jobs/{newer_job_id}/sources/other.mp4",
                    all_candidates={"reused_source": path},
                    assembly_plan={},
                    created_at=datetime.now(UTC) - timedelta(days=10),
                    updated_at=datetime.now(UTC) - timedelta(days=10),
                ),
            ]
        )
        db.commit()
    manifest_id, entry_id, _ = approved_manifest(
        creator_id=creator_id,
        job_id=old_job_id,
        path=path,
    )
    delete = MagicMock()
    monkeypatch.setattr(storage_retention.storage, "delete_object_generation", delete)

    try:
        result = storage_retention.execute_retention_manifest(str(manifest_id))
        assert result == {"deleted": 0, "skipped": 1, "failed": 0}
        delete.assert_not_called()
        with sync_session() as db:
            assert db.get(StorageRetentionEntry, entry_id).status == "skipped"
    finally:
        with sync_session() as db:
            db.execute(
                text("DELETE FROM jobs WHERE id IN (:old_id, :new_id)"),
                {
                    "old_id": str(old_job_id),
                    "new_id": str(newer_job_id),
                },
            )
            db.execute(text("DELETE FROM users WHERE id = :id"), {"id": str(creator_id)})
            db.commit()


@pytest.mark.parametrize("protected_field", ["source", "preview"])
def test_executor_protects_a_registered_plan_item_asset(
    approved_manifest,
    monkeypatch: pytest.MonkeyPatch,
    protected_field: str,
) -> None:
    creator_id = uuid.uuid4()
    persona_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    item_id = uuid.uuid4()
    asset_id = uuid.uuid4()
    source_path = f"users/{creator_id}/plan/{item_id}/pool/source.mp4"
    preview_path = f"{source_path}.preview.jpg"
    protected_path = source_path if protected_field == "source" else preview_path
    with sync_session() as db:
        db.add(User(id=creator_id, email=f"retention-asset-{creator_id}@test.local"))
        db.flush()
        db.add(Persona(id=persona_id, user_id=creator_id, persona_status="ready"))
        db.flush()
        db.add(ContentPlan(id=plan_id, user_id=creator_id, persona_id=persona_id))
        db.flush()
        db.add(PlanItem(id=item_id, content_plan_id=plan_id, position=0, idea="test"))
        db.flush()
        db.add(
            PlanItemAsset(
                id=asset_id,
                plan_item_id=item_id,
                user_id=creator_id,
                gcs_path=source_path,
                preview_gcs_path=preview_path,
                kind="video",
                status="ready",
            )
        )
        db.commit()
    manifest_id, entry_id, _ = approved_manifest(
        creator_id=creator_id,
        path=protected_path,
    )
    delete = MagicMock()
    monkeypatch.setattr(storage_retention.storage, "delete_object_generation", delete)

    try:
        result = storage_retention.execute_retention_manifest(str(manifest_id))

        assert result == {"deleted": 0, "skipped": 1, "failed": 0}
        delete.assert_not_called()
        with sync_session() as db:
            assert db.get(StorageRetentionEntry, entry_id).status == "skipped"
    finally:
        with sync_session() as db:
            db.execute(text("DELETE FROM plan_item_assets WHERE id = :id"), {"id": asset_id})
            db.execute(text("DELETE FROM plan_items WHERE id = :id"), {"id": item_id})
            db.execute(text("DELETE FROM content_plans WHERE id = :id"), {"id": plan_id})
            db.execute(text("DELETE FROM personas WHERE id = :id"), {"id": persona_id})
            db.execute(text("DELETE FROM users WHERE id = :id"), {"id": creator_id})
            db.commit()


def test_executor_rechecks_the_originating_job_retention_window(
    approved_manifest, monkeypatch: pytest.MonkeyPatch
) -> None:
    creator_id = uuid.uuid4()
    job_id = uuid.uuid4()
    path = f"users/{creator_id}/jobs/{job_id}/sources/restored.mp4"
    with sync_session() as db:
        db.add(User(id=creator_id, email=f"retention-{creator_id}@test.local"))
        db.add(
            Job(
                id=job_id,
                user_id=creator_id,
                status="done",
                job_type="template",
                raw_storage_path=path,
                assembly_plan={},
                created_at=NOW - timedelta(days=100),
                updated_at=datetime.now(UTC) - timedelta(days=10),
            )
        )
        db.commit()
    manifest_id, entry_id, _ = approved_manifest(
        creator_id=creator_id,
        job_id=job_id,
        path=path,
    )
    delete = MagicMock()
    monkeypatch.setattr(storage_retention.storage, "delete_object_generation", delete)

    try:
        result = storage_retention.execute_retention_manifest(str(manifest_id))
        assert result == {"deleted": 0, "skipped": 1, "failed": 0}
        delete.assert_not_called()
        with sync_session() as db:
            assert db.get(StorageRetentionEntry, entry_id).status == "skipped"
    finally:
        with sync_session() as db:
            db.execute(text("DELETE FROM jobs WHERE id = :id"), {"id": str(job_id)})
            db.execute(text("DELETE FROM users WHERE id = :id"), {"id": str(creator_id)})
            db.commit()


def test_executor_protects_a_persona_onboarding_source(
    approved_manifest, monkeypatch: pytest.MonkeyPatch
) -> None:
    creator_id = uuid.uuid4()
    job_id = uuid.uuid4()
    path = f"generative-jobs/{job_id}/sources/onboarding.mp4"
    with sync_session() as db:
        db.add(User(id=creator_id, email=f"retention-{creator_id}@test.local"))
        db.add(
            Job(
                id=job_id,
                user_id=creator_id,
                status="done",
                job_type="generative",
                raw_storage_path=path,
                assembly_plan={},
                created_at=NOW - timedelta(days=100),
                updated_at=NOW - timedelta(days=100),
            )
        )
        db.add(
            Persona(
                user_id=creator_id,
                questionnaire={"onboarding_clip_paths": [path]},
            )
        )
        db.commit()
    manifest_id, entry_id, _ = approved_manifest(
        creator_id=creator_id,
        job_id=job_id,
        path=path,
    )
    delete = MagicMock()
    monkeypatch.setattr(storage_retention.storage, "delete_object_generation", delete)

    try:
        result = storage_retention.execute_retention_manifest(str(manifest_id))
        assert result == {"deleted": 0, "skipped": 1, "failed": 0}
        delete.assert_not_called()
        with sync_session() as db:
            assert db.get(StorageRetentionEntry, entry_id).status == "skipped"
    finally:
        with sync_session() as db:
            db.execute(text("DELETE FROM jobs WHERE id = :id"), {"id": str(job_id)})
            db.execute(text("DELETE FROM users WHERE id = :id"), {"id": str(creator_id)})
            db.commit()
