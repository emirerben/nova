"""Database-backed, generation-pinned storage retention manifests.

The daily task is report-only by default. It never deletes an object until the
manifest has soaked for seven days, an operator has approved that exact
manifest, and the independent delete kill switch is enabled.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import func, select, text, tuple_, update

from app import storage
from app.config import settings
from app.database import sync_session
from app.models import (
    ContentPlan,
    Job,
    JobClip,
    Persona,
    PlanItem,
    PlanItemAsset,
    StorageRetentionEntry,
    StorageRetentionManifest,
    TikTokPublication,
)
from app.services.job_storage_paths import (
    JOB_OUTPUT_PREFIXES,
    normalize_job_storage_path,
    project_media_reference_lock_key,
)
from app.worker import celery_app

log = structlog.get_logger()

_KNOWN_PREFIXES = (
    "users/",
    "generative-jobs/",
    "job-posters/",
    "auto-music-jobs/",
    "music-jobs/",
    "jobs/",
    "dev-user/",
    "staging/",
    "voiceover-uploads/",
)
_EXECUTION_LEASE = timedelta(minutes=10)
_MIN_WARNING_LEAD = timedelta(days=7)


def _walk_paths(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for child in value.values():
            found.update(_walk_paths(child))
    elif isinstance(value, (list, tuple, set)):
        for child in value:
            found.update(_walk_paths(child))
    elif isinstance(value, str):
        normalized = normalize_job_storage_path(value)
        if normalized and (
            normalized.startswith(_KNOWN_PREFIXES) or _is_legacy_authenticated_job_path(normalized)
        ):
            found.add(normalized)
    return found


def _is_legacy_authenticated_job_path(path: str) -> bool:
    """Recognize the historical ``{user_uuid}/{job_uuid}/...`` namespace."""

    parts = path.split("/", 2)
    if len(parts) != 3:
        return False
    try:
        uuid.UUID(parts[0])
        uuid.UUID(parts[1])
    except ValueError:
        return False
    return bool(parts[2])


def _protected_references(db: Any) -> tuple[set[str], set[uuid.UUID]]:
    """Return current project/publication references and current Job identities."""

    paths: set[str] = set()
    active_jobs: set[uuid.UUID] = set()
    for row in db.execute(
        select(
            PlanItem.current_job_id,
            PlanItem.clip_gcs_paths,
            PlanItem.clip_assignments,
            PlanItem.voiceover_gcs_path,
        )
    ):
        if row.current_job_id is not None:
            active_jobs.add(row.current_job_id)
        paths.update(
            _walk_paths((row.clip_gcs_paths, row.clip_assignments, row.voiceover_gcs_path))
        )
    for row in db.execute(select(ContentPlan.seed_clip_paths, ContentPlan.pool)):
        paths.update(_walk_paths((row.seed_clip_paths, row.pool)))
    for row in db.execute(select(PlanItemAsset.gcs_path, PlanItemAsset.preview_gcs_path)):
        paths.update(_walk_paths((row.gcs_path, row.preview_gcs_path)))
    for questionnaire in db.scalars(select(Persona.questionnaire)):
        paths.update(_walk_paths(questionnaire))
    for row in db.execute(
        select(
            TikTokPublication.source_object_path,
            TikTokPublication.snapshot_object_path,
        ).where(TikTokPublication.visibility_status.in_(("unknown", "draft", "private", "public")))
    ):
        paths.update(_walk_paths((row.source_object_path, row.snapshot_object_path)))
    # A current job protects all of its own inputs/outputs, including a path
    # that an older inactive job also happens to reference. Without this pass,
    # only the active job ID was protected and a shared generation could enter
    # an older job's deletion manifest.
    if active_jobs:
        for job in db.scalars(select(Job).where(Job.id.in_(active_jobs))):
            paths.update(_walk_paths((job.raw_storage_path, job.all_candidates, job.assembly_plan)))
        for clip in db.scalars(select(JobClip).where(JobClip.job_id.in_(active_jobs))):
            paths.update(_walk_paths((clip.video_path, clip.thumbnail_path)))
    return paths, active_jobs


def _owner_source_reference_times(
    db: Any, owner_ids: set[uuid.UUID]
) -> dict[tuple[uuid.UUID, str], datetime]:
    """Latest Job update that still names each owner-scoped source path."""

    latest: dict[tuple[uuid.UUID, str], datetime] = {}
    if not owner_ids:
        return latest
    for job in db.scalars(select(Job).where(Job.user_id.in_(owner_ids))):
        for path in _walk_paths((job.raw_storage_path, job.all_candidates)):
            key = (job.user_id, path)
            latest[key] = max(latest.get(key, job.updated_at), job.updated_at)
    return latest


def _prior_warning_times(
    db: Any, owner_ids: set[uuid.UUID]
) -> dict[tuple[uuid.UUID, str, str], datetime]:
    """Earliest durable creator warning for an immutable object generation."""

    warned: dict[tuple[uuid.UUID, str, str], datetime] = {}
    if not owner_ids:
        return warned
    rows = db.execute(
        select(
            StorageRetentionEntry.creator_id,
            StorageRetentionEntry.object_path,
            StorageRetentionEntry.object_generation,
            StorageRetentionEntry.created_at,
        ).where(
            StorageRetentionEntry.creator_id.in_(owner_ids),
            StorageRetentionEntry.action == "warn",
        )
    )
    for creator_id, path, generation, created_at in rows:
        if creator_id is None:
            continue
        key = (creator_id, path, generation)
        warned[key] = min(warned.get(key, created_at), created_at)
    return warned


def _entry_is_protected(db: Any, entry: StorageRetentionEntry) -> bool:
    """Recheck one candidate against its owner's live references.

    The manifest build may scan the full corpus once, but destructive execution
    must not repeat that global scan for every object. Storage paths are
    owner-scoped, so this narrows the fresh check to the candidate creator and
    its currently selected jobs/publications.
    """

    if entry.job_id is not None and db.scalar(
        select(PlanItem.id).where(PlanItem.current_job_id == entry.job_id).limit(1)
    ):
        return True
    if entry.creator_id is None:
        # Old/malformed entries cannot be proven unreferenced safely.
        return True

    owner_id = entry.creator_id
    path = entry.object_path
    active_job_ids: set[uuid.UUID] = set()
    for row in db.execute(
        select(
            PlanItem.current_job_id,
            PlanItem.clip_gcs_paths,
            PlanItem.clip_assignments,
            PlanItem.voiceover_gcs_path,
        )
        .join(ContentPlan, ContentPlan.id == PlanItem.content_plan_id)
        .where(ContentPlan.user_id == owner_id)
    ):
        if row.current_job_id is not None:
            active_job_ids.add(row.current_job_id)
        if path in _walk_paths((row.clip_gcs_paths, row.clip_assignments, row.voiceover_gcs_path)):
            return True
    for row in db.execute(
        select(ContentPlan.seed_clip_paths, ContentPlan.pool).where(ContentPlan.user_id == owner_id)
    ):
        if path in _walk_paths((row.seed_clip_paths, row.pool)):
            return True
    for row in db.execute(
        select(PlanItemAsset.gcs_path, PlanItemAsset.preview_gcs_path).where(
            PlanItemAsset.user_id == owner_id
        )
    ):
        if path in _walk_paths((row.gcs_path, row.preview_gcs_path)):
            return True
    questionnaire = db.scalar(select(Persona.questionnaire).where(Persona.user_id == owner_id))
    if path in _walk_paths(questionnaire):
        return True
    for row in db.execute(
        select(TikTokPublication.source_object_path, TikTokPublication.snapshot_object_path).where(
            TikTokPublication.user_id == owner_id,
            TikTokPublication.visibility_status.in_(("unknown", "draft", "private", "public")),
        )
    ):
        if path in _walk_paths((row.source_object_path, row.snapshot_object_path)):
            return True
    if active_job_ids:
        for job in db.scalars(select(Job).where(Job.id.in_(active_job_ids))):
            if path in _walk_paths((job.raw_storage_path, job.all_candidates, job.assembly_plan)):
                return True
        for clip in db.scalars(select(JobClip).where(JobClip.job_id.in_(active_job_ids))):
            if path in _walk_paths((clip.video_path, clip.thumbnail_path)):
                return True
    # A source referenced by a job whose retention window is still live stays
    # protected, including the originating job if it changed after manifest
    # construction. Merely naming the path is not enough: once the applicable
    # deadline passes, the originating job must still be deletable.
    now = datetime.now(UTC)
    for job in db.scalars(select(Job).where(Job.user_id == owner_id)):
        if path in _walk_paths((job.raw_storage_path, job.all_candidates)) and (
            job.updated_at + timedelta(days=settings.storage_retention_inactive_days) > now
        ):
            return True
        if path in _walk_paths(job.assembly_plan) and (
            job.updated_at + timedelta(days=settings.storage_retention_final_days) > now
        ):
            return True
    for clip, job_updated_at in db.execute(
        select(JobClip, Job.updated_at)
        .join(Job, Job.id == JobClip.job_id)
        .where(Job.user_id == owner_id)
    ):
        if path in _walk_paths((clip.video_path, clip.thumbnail_path)) and (
            job_updated_at + timedelta(days=settings.storage_retention_final_days) > now
        ):
            return True
    return False


def _previous_scan_cursor(db: Any) -> tuple[datetime, uuid.UUID] | None:
    summary = db.scalar(
        select(StorageRetentionManifest.summary_json)
        .where(StorageRetentionManifest.status.in_(("completed", "rejected")))
        .order_by(StorageRetentionManifest.generated_at.desc())
        .limit(1)
    )
    if not isinstance(summary, dict):
        return None
    try:
        updated_at = datetime.fromisoformat(str(summary["cursor_updated_at"]))
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=UTC)
        return updated_at, uuid.UUID(str(summary["cursor_job_id"]))
    except (KeyError, TypeError, ValueError):
        return None


def _job_references(db: Any, job: Job) -> tuple[set[str], set[str]]:
    sources = _walk_paths((job.raw_storage_path, job.all_candidates))
    current = _walk_paths((job.assembly_plan, job.raw_storage_path, job.all_candidates))
    for clip in db.scalars(select(JobClip).where(JobClip.job_id == job.id)):
        current.update(_walk_paths((clip.video_path, clip.thumbnail_path)))
    return sources, current


def _metadata_for_job(job: Job, source_paths: set[str]) -> tuple[list[storage.ObjectMetadata], int]:
    by_identity: dict[tuple[str, str], storage.ObjectMetadata] = {}
    errors = 0
    prefixes = [
        *(prefix_template.format(job_id=job.id) for prefix_template in JOB_OUTPUT_PREFIXES),
        f"users/{job.user_id}/jobs/{job.id}/",
        f"{job.user_id}/{job.id}/",
    ]
    for prefix in prefixes:
        try:
            for metadata in storage.list_object_metadata(prefix):
                by_identity[(metadata.path, metadata.generation)] = metadata
        except Exception as exc:  # noqa: BLE001 - report remains partial and explicit
            errors += 1
            log.warning(
                "storage_retention.list_failed",
                job_id=str(job.id),
                prefix=prefix,
                error_type=type(exc).__name__,
            )
    for path in source_paths:
        if any(identity[0] == path for identity in by_identity):
            continue
        try:
            metadata = storage.object_metadata_once(path, timeout_s=3.0)
        except FileNotFoundError:
            continue
        except Exception as exc:  # noqa: BLE001
            errors += 1
            log.warning(
                "storage_retention.metadata_failed",
                job_id=str(job.id),
                path=path,
                error_type=type(exc).__name__,
            )
            continue
        by_identity[(metadata.path, metadata.generation)] = metadata
    return list(by_identity.values()), errors


def _candidate(
    *,
    metadata: storage.ObjectMetadata,
    job: Job,
    now: datetime,
    protected_paths: set[str],
    active_jobs: set[uuid.UUID],
    source_paths: set[str],
    current_paths: set[str],
    latest_source_reference_at: datetime | None = None,
    warned_at: datetime | None = None,
) -> tuple[str, str, datetime] | None:
    created_at = metadata.created_at or job.updated_at or job.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    inactive = job.id not in active_jobs
    if metadata.path in protected_paths or not inactive:
        return None
    if metadata.path in source_paths or any(
        marker in metadata.path for marker in ("/sources/", "/preprocessed/", "/base_")
    ):
        delete_at = max(
            created_at + timedelta(days=settings.storage_retention_inactive_days),
            job.updated_at + timedelta(days=settings.storage_retention_inactive_days),
            (
                latest_source_reference_at
                + timedelta(days=settings.storage_retention_inactive_days)
                if latest_source_reference_at is not None
                else job.updated_at + timedelta(days=settings.storage_retention_inactive_days)
            ),
        )
        warn_at = delete_at - timedelta(
            days=(
                settings.storage_retention_inactive_days - settings.storage_retention_warning_days
            )
        )
        if now >= delete_at:
            if warned_at is None:
                return "warn", "inactive_source_warning", delete_at
            deletion_not_before = max(delete_at, warned_at + _MIN_WARNING_LEAD)
            if now < deletion_not_before:
                return None
            return "delete", "inactive_source_or_editable", deletion_not_before
        if now >= warn_at:
            if warned_at is not None:
                return None
            return "warn", "inactive_source_warning", delete_at
        return None
    if metadata.path in current_paths:
        retain_until = max(
            created_at + timedelta(days=settings.storage_retention_final_days),
            job.updated_at + timedelta(days=settings.storage_retention_final_days),
        )
        if now >= retain_until:
            reason = "poster" if metadata.path.startswith("job-posters/") else "latest_final"
            return "delete", reason, retain_until
        return None
    delete_at = created_at + timedelta(days=settings.storage_retention_superseded_days)
    if now >= delete_at:
        return "delete", "superseded_derivative", delete_at
    return None


def build_retention_manifest(*, now: datetime | None = None) -> str | None:
    """Build one report-only manifest, or advance the current report to approval."""

    if not settings.storage_retention_enabled:
        return None
    now = now or datetime.now(UTC)
    with sync_session() as db:
        current = db.scalar(
            select(StorageRetentionManifest)
            .where(
                StorageRetentionManifest.status.in_(
                    ("report_only", "pending_approval", "approved", "executing")
                )
            )
            .order_by(StorageRetentionManifest.generated_at.desc())
            .limit(1)
        )
        if current is not None:
            if current.status == "report_only" and now >= current.report_only_until:
                current.status = "pending_approval"
                db.commit()
            return str(current.id)
        protected_paths, active_jobs = _protected_references(db)
        cursor = _previous_scan_cursor(db)
        eligible_before = now - timedelta(days=settings.storage_retention_superseded_days)
        job_query = select(Job).where(Job.updated_at <= eligible_before)
        if cursor is not None:
            job_query = job_query.where(tuple_(Job.updated_at, Job.id) > cursor)
        jobs = db.scalars(
            job_query.order_by(Job.updated_at.asc(), Job.id.asc()).limit(
                settings.storage_retention_scan_jobs
            )
        ).all()
        wrapped = False
        if cursor is not None and not jobs:
            wrapped = True
            jobs = db.scalars(
                select(Job)
                .where(Job.updated_at <= eligible_before)
                .order_by(Job.updated_at.asc(), Job.id.asc())
                .limit(settings.storage_retention_scan_jobs)
            ).all()
        # Materialize database state, then release the transaction/connection
        # before any remote object listing or metadata request.
        scan_jobs = [(job, *_job_references(db, job)) for job in jobs]
        owner_ids = {job.user_id for job in jobs}
        source_reference_times = _owner_source_reference_times(db, owner_ids)
        prior_warning_times = _prior_warning_times(db, owner_ids)

    counts: dict[str, Any] = {
        "warn": 0,
        "delete": 0,
        "bytes": 0,
        "scan_errors": 0,
        "jobs": len(jobs),
    }
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for job, source_paths, current_paths in scan_jobs:
        metadata_rows, errors = _metadata_for_job(job, source_paths)
        counts["scan_errors"] += errors
        for metadata in metadata_rows:
            identity = (metadata.path, metadata.generation)
            if identity in seen:
                continue
            seen.add(identity)
            candidate = _candidate(
                metadata=metadata,
                job=job,
                now=now,
                protected_paths=protected_paths,
                active_jobs=active_jobs,
                source_paths=source_paths,
                current_paths=current_paths,
                latest_source_reference_at=source_reference_times.get((job.user_id, metadata.path)),
                warned_at=prior_warning_times.get(
                    (job.user_id, metadata.path, metadata.generation)
                ),
            )
            if candidate is None:
                continue
            action, reason, eligible_at = candidate
            candidates.append(
                {
                    "job_id": job.id,
                    "creator_id": job.user_id,
                    "object_path": metadata.path,
                    "object_generation": metadata.generation,
                    "action": action,
                    "reason": reason,
                    "eligible_at": eligible_at,
                    "size_bytes": metadata.size,
                }
            )
            counts[action] += 1
            counts["bytes"] += metadata.size
    if jobs:
        counts["cursor_updated_at"] = jobs[-1].updated_at.isoformat()
        counts["cursor_job_id"] = str(jobs[-1].id)
    counts["cursor_wrapped"] = wrapped

    with sync_session() as db:
        # A second beat/task may have completed the remote scan concurrently.
        # Serialize only the short publish transaction (never the remote scan)
        # so both tasks cannot observe "no active manifest" and insert one.
        db.execute(
            text(
                "SELECT pg_advisory_xact_lock("
                "hashtextextended('storage_retention_manifest_build', 0))"
            )
        )
        current = db.scalar(
            select(StorageRetentionManifest)
            .where(
                StorageRetentionManifest.status.in_(
                    ("report_only", "pending_approval", "approved", "executing")
                )
            )
            .order_by(StorageRetentionManifest.generated_at.desc())
            .limit(1)
        )
        if current is not None:
            return str(current.id)
        manifest = StorageRetentionManifest(
            status="report_only",
            generated_at=now,
            report_only_until=now + timedelta(days=settings.storage_retention_report_only_days),
            summary_json=counts,
        )
        db.add(manifest)
        db.flush()
        for values in candidates:
            db.add(StorageRetentionEntry(manifest_id=manifest.id, **values))
        db.commit()
        log.info("storage_retention.manifest_built", manifest_id=str(manifest.id), **counts)
        return str(manifest.id)


def execute_retention_manifest(manifest_id: str) -> dict[str, int]:
    """Execute an approved manifest with a fresh DB-reference and generation check."""

    if not settings.storage_retention_delete_enabled:
        return {"deleted": 0, "skipped": 0, "failed": 0}
    now = datetime.now(UTC)
    manifest_uuid = uuid.UUID(manifest_id)
    lease_id = uuid.uuid4()
    results = {"deleted": 0, "skipped": 0, "failed": 0}
    with sync_session() as db:
        manifest = db.scalar(
            select(StorageRetentionManifest)
            .where(StorageRetentionManifest.id == manifest_uuid)
            .with_for_update()
        )
        if (
            manifest is None
            or manifest.approved_at is None
            or now < manifest.report_only_until
            or (
                manifest.status == "executing"
                and manifest.execution_lease_expires_at is not None
                and manifest.execution_lease_expires_at > now
            )
            or manifest.status not in ("approved", "executing")
        ):
            return results
        manifest.status = "executing"
        manifest.execution_lease_id = lease_id
        manifest.execution_lease_expires_at = now + _EXECUTION_LEASE
        entry_ids = db.scalars(
            select(StorageRetentionEntry.id).where(
                StorageRetentionEntry.manifest_id == manifest_uuid,
                StorageRetentionEntry.action == "delete",
                StorageRetentionEntry.status.in_(("pending", "deleting")),
            )
        ).all()
        db.commit()

    def release_claim() -> None:
        with sync_session() as db:
            db.execute(
                update(StorageRetentionManifest)
                .where(
                    StorageRetentionManifest.id == manifest_uuid,
                    StorageRetentionManifest.status == "executing",
                    StorageRetentionManifest.execution_lease_id == lease_id,
                )
                .values(
                    status="approved",
                    execution_lease_id=None,
                    execution_lease_expires_at=None,
                )
            )
            db.commit()

    try:
        for entry_id in entry_ids:
            with sync_session() as db:
                manifest = db.get(StorageRetentionManifest, manifest_uuid)
                entry = db.get(StorageRetentionEntry, entry_id)
                if (
                    manifest is None
                    or manifest.status != "executing"
                    or manifest.execution_lease_id != lease_id
                    or entry is None
                    or entry.status not in ("pending", "deleting")
                ):
                    return results
                if entry.creator_id is None:
                    entry.status = "skipped"
                    entry.error_detail = "creator_owner_missing"
                    manifest.execution_lease_expires_at = datetime.now(UTC) + _EXECUTION_LEASE
                    db.commit()
                    results["skipped"] += 1
                    continue
                # Migration 0103 makes every reference-bearing table writer
                # acquire this same owner-scoped lock. The short transaction
                # below rechecks references and durably marks the exact
                # generation as deleting before any irreversible GCS call.
                db.execute(
                    select(
                        func.pg_advisory_xact_lock(
                            project_media_reference_lock_key(entry.creator_id)
                        )
                    )
                )
                # A resumed `deleting` row may already have completed its
                # remote delete before a worker crash. Its durable tombstone
                # has prevented new references, so resume it idempotently.
                if entry.status == "pending" and _entry_is_protected(db, entry):
                    entry.status = "skipped"
                    db.execute(
                        update(StorageRetentionEntry)
                        .where(
                            StorageRetentionEntry.action == "warn",
                            StorageRetentionEntry.status == "pending",
                            StorageRetentionEntry.object_path == entry.object_path,
                            StorageRetentionEntry.object_generation == entry.object_generation,
                        )
                        .values(status="skipped")
                    )
                    manifest.execution_lease_expires_at = datetime.now(UTC) + _EXECUTION_LEASE
                    db.commit()
                    results["skipped"] += 1
                    continue
                warned_at = db.scalar(
                    select(StorageRetentionEntry.created_at)
                    .where(
                        StorageRetentionEntry.creator_id == entry.creator_id,
                        StorageRetentionEntry.object_path == entry.object_path,
                        StorageRetentionEntry.object_generation == entry.object_generation,
                        StorageRetentionEntry.action == "warn",
                        StorageRetentionEntry.created_at <= datetime.now(UTC) - _MIN_WARNING_LEAD,
                    )
                    .order_by(StorageRetentionEntry.created_at.asc())
                    .limit(1)
                )
                if (
                    entry.status == "pending"
                    and entry.reason == "inactive_source_or_editable"
                    and warned_at is None
                ):
                    entry.status = "skipped"
                    entry.error_detail = "creator_warning_lead_time_missing"
                    manifest.execution_lease_expires_at = datetime.now(UTC) + _EXECUTION_LEASE
                    db.commit()
                    results["skipped"] += 1
                    continue
                path = entry.object_path
                generation = entry.object_generation
                eligible_at = entry.eligible_at
                if entry.status == "pending":
                    if now < eligible_at:
                        entry.status = "skipped"
                        manifest.execution_lease_expires_at = datetime.now(UTC) + _EXECUTION_LEASE
                        db.commit()
                        results["skipped"] += 1
                        continue
                    entry.status = "deleting"
                    entry.deleted_at = datetime.now(UTC)
                    entry.error_detail = None
                    manifest.execution_lease_expires_at = datetime.now(UTC) + _EXECUTION_LEASE
                    db.commit()

            # The durable `deleting` tombstone is now visible to every
            # reference-writing trigger. If this worker dies after GCS accepts
            # the delete, the next execution resumes and FileNotFound safely
            # finalizes the same generation.
            terminal_status: str | None = None
            retire_status: str | None = None
            error_detail: str | None = None
            try:
                metadata = storage.object_metadata_once(path, timeout_s=3.0)
                if metadata.generation != generation:
                    terminal_status = "skipped"
                    retire_status = "skipped"
                    results["skipped"] += 1
                else:
                    storage.delete_object_generation(
                        path,
                        generation=generation,
                        timeout_s=3.0,
                    )
                    terminal_status = "deleted"
                    retire_status = "deleted"
                    results["deleted"] += 1
            except FileNotFoundError:
                terminal_status = "deleted"
                retire_status = "deleted"
                results["deleted"] += 1
            except Exception as exc:  # noqa: BLE001
                error_detail = type(exc).__name__
                results["failed"] += 1

            with sync_session() as db:
                manifest = db.get(StorageRetentionManifest, manifest_uuid)
                entry = db.get(StorageRetentionEntry, entry_id)
                if entry is None or entry.status != "deleting":
                    return results
                if terminal_status is not None:
                    entry.status = terminal_status
                    entry.error_detail = None
                    if terminal_status == "skipped":
                        entry.deleted_at = None
                    else:
                        # Refresh the tombstone at the moment absence is
                        # confirmed. A deleting retry may run long after the
                        # first, outcome-unknown attempt; retaining that old
                        # timestamp would let a pre-validation writer through
                        # the trigger's bounded post-delete grace window.
                        entry.deleted_at = datetime.now(UTC)
                else:
                    # A timeout/error cannot prove whether GCS accepted the
                    # generation-pinned delete. Keep the tombstone until a
                    # retry observes either that exact generation or absence;
                    # clearing it here could admit a stale reference after an
                    # outcome-unknown delete.
                    entry.error_detail = error_detail
                if retire_status is not None:
                    db.execute(
                        update(StorageRetentionEntry)
                        .where(
                            StorageRetentionEntry.action == "warn",
                            StorageRetentionEntry.status == "pending",
                            StorageRetentionEntry.object_path == path,
                            StorageRetentionEntry.object_generation == generation,
                        )
                        .values(status=retire_status)
                    )
                if (
                    manifest is not None
                    and manifest.status == "executing"
                    and manifest.execution_lease_id == lease_id
                ):
                    manifest.execution_lease_expires_at = datetime.now(UTC) + _EXECUTION_LEASE
                db.commit()
    except BaseException:
        # Cooperative worker shutdown releases immediately; an abrupt process
        # loss is recovered by the durable lease expiry.
        release_claim()
        raise

    with sync_session() as db:
        manifest = db.scalar(
            select(StorageRetentionManifest)
            .where(StorageRetentionManifest.id == manifest_uuid)
            .with_for_update()
        )
        if (
            manifest is None
            or manifest.status != "executing"
            or manifest.execution_lease_id != lease_id
        ):
            return results
        manifest.status = "approved" if results["failed"] else "completed"
        manifest.executed_at = None if results["failed"] else now
        manifest.execution_lease_id = None
        manifest.execution_lease_expires_at = None
        manifest.summary_json = {**(manifest.summary_json or {}), "execution": results}
        db.commit()
    log.info("storage_retention.manifest_executed", manifest_id=manifest_id, **results)
    return results


@celery_app.task(
    name="tasks.sweep_storage_retention",
    max_retries=0,
    soft_time_limit=240,
    time_limit=300,
)
def sweep_storage_retention() -> str | None:
    manifest_id = build_retention_manifest()
    if manifest_id and settings.storage_retention_delete_enabled:
        execute_retention_manifest(manifest_id)
    return manifest_id
