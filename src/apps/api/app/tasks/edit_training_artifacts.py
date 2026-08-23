"""Generation-pinned final-render retention and consent-revocation purge."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

import structlog
from sqlalchemy import and_, or_, select

from app.database import sync_session
from app.models import EditArtifact, Job, TrainingArtifactRetentionEvent
from app.services.edit_artifacts import (
    get_or_create_artifact,
    get_or_create_purge_event,
    load_render_capture_snapshot,
    mark_retention_failed,
    mark_retention_started,
    mark_retention_succeeded,
)
from app.services.training_eligibility import artifact_is_eligible
from app.worker import celery_app

log = structlog.get_logger()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@celery_app.task(
    name="tasks.backfill_edit_training_artifacts",
    bind=True,
    autoretry_for=(),
    max_retries=0,
    soft_time_limit=300,
    time_limit=600,
)
def backfill_edit_training_artifacts(
    self,
    creator_id: str,
    batch_size: int = 300,
    before_created_at: str | None = None,
    before_job_id: str | None = None,
) -> dict[str, object]:
    """Queue every discoverable historic render in bounded keyset pages."""
    creator_uuid = uuid.UUID(creator_id)
    bounded_batch = max(1, min(int(batch_size), 1000))
    with sync_session() as db:
        query = select(Job).where(
            Job.user_id == creator_uuid,
            Job.content_plan_item_id.is_not(None),
        )
        if before_created_at and before_job_id:
            cursor_at = datetime.fromisoformat(before_created_at)
            cursor_id = uuid.UUID(before_job_id)
            query = query.where(
                or_(
                    Job.created_at < cursor_at,
                    and_(Job.created_at == cursor_at, Job.id < cursor_id),
                )
            )
        jobs = (
            db.execute(query.order_by(Job.created_at.desc(), Job.id.desc()).limit(bounded_batch))
            .scalars()
            .all()
        )
    queued = 0
    for job in jobs:
        for variant in (job.assembly_plan or {}).get("variants") or []:
            if not isinstance(variant, dict):
                continue
            variant_id = variant.get("variant_id")
            generation = variant.get("render_generation_id")
            if (
                variant.get("render_status") == "ready"
                and variant.get("video_path")
                and isinstance(variant_id, str)
                and isinstance(generation, str)
            ):
                capture_edit_training_artifact.delay(str(job.id), variant_id, generation)
                queued += 1
    continuation_queued = len(jobs) == bounded_batch and bool(jobs)
    if continuation_queued:
        last = jobs[-1]
        backfill_edit_training_artifacts.delay(
            creator_id,
            bounded_batch,
            last.created_at.isoformat(),
            str(last.id),
        )
    return {
        "status": "queued",
        "count": queued,
        "jobs_scanned": len(jobs),
        "continuation_queued": continuation_queued,
    }


def _record_derivative(
    db,
    *,
    parent: EditArtifact,
    kind: str,
    artifact_id: uuid.UUID,
    metadata,
    content_hash: str,
) -> None:  # noqa: ANN001
    existing = db.execute(
        select(EditArtifact.id).where(
            EditArtifact.parent_artifact_id == parent.id,
            EditArtifact.artifact_kind == kind,
            EditArtifact.render_generation_id == parent.render_generation_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return
    receipt = {
        "schema_version": "edit-derivative-v1",
        "revision_hash": parent.render_receipt.get("revision_hash"),
        "parent_artifact_id": str(parent.id),
        "kind": kind,
    }
    receipt_hash = hashlib.sha256(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    artifact = EditArtifact(
        id=artifact_id,
        creator_id=parent.creator_id,
        plan_item_id=parent.plan_item_id,
        job_id=parent.job_id,
        parent_artifact_id=parent.id,
        variant_id=parent.variant_id,
        render_generation_id=parent.render_generation_id,
        artifact_kind=kind,
        artifact_schema_version=parent.artifact_schema_version,
        proposal_version=parent.proposal_version,
        media_digest=parent.media_digest,
        direction_snapshot=parent.direction_snapshot,
        render_hash=content_hash,
        render_receipt_hash=receipt_hash,
        render_receipt_schema_version="edit-derivative-v1",
        render_receipt=receipt,
        prompt_version=parent.prompt_version,
        effective_model=parent.effective_model,
        media_manifest=None,
        storage_path=metadata.path,
        storage_generation=metadata.generation,
        storage_content_hash=content_hash,
        storage_size_bytes=metadata.size,
        content_type=metadata.content_type,
        capture_origin=parent.capture_origin,
        eligibility_basis=parent.eligibility_basis,
        consent_event_id=parent.consent_event_id,
        internal_grant_id=parent.internal_grant_id,
        creator_split=parent.creator_split,
        plan_item_split=parent.plan_item_split,
    )
    db.add(artifact)
    db.add(
        TrainingArtifactRetentionEvent(
            id=uuid.uuid4(),
            creator_id=parent.creator_id,
            artifact_id=artifact_id,
            event_type="ready",
            status="succeeded",
            storage_path=metadata.path,
            storage_generation=metadata.generation,
            content_hash=content_hash,
            idempotency_key=f"ready:{metadata.generation}",
            completed_at=datetime.now(UTC),
        )
    )
    db.commit()


def _build_review_derivatives(parent_id: uuid.UUID, copied) -> None:  # noqa: ANN001
    """Best-effort poster/contact sheet derived only from the retained final."""
    from app import storage  # noqa: PLC0415

    with sync_session() as db:
        parent = db.get(EditArtifact, parent_id)
        if parent is None or not artifact_is_eligible(db, parent):
            return
        creator_id = parent.creator_id
    with tempfile.TemporaryDirectory(prefix="edit-feedback-derivatives-") as tmp_dir:
        video = Path(tmp_dir) / "final.mp4"
        poster = Path(tmp_dir) / "poster.jpg"
        sheet = Path(tmp_dir) / "contact-sheet.jpg"
        storage.download_generation_to_file(
            copied.path,
            str(video),
            generation=copied.generation,
        )
        commands = {
            "poster": [
                "ffmpeg",
                "-y",
                "-ss",
                "0.1",
                "-i",
                str(video),
                "-frames:v",
                "1",
                "-vf",
                "scale=540:-2",
                str(poster),
            ],
            "contact_sheet": [
                "ffmpeg",
                "-y",
                "-i",
                str(video),
                "-vf",
                "thumbnail=60,scale=270:-2,tile=3x2",
                "-frames:v",
                "1",
                str(sheet),
            ],
        }
        for kind, command in commands.items():
            try:
                with sync_session() as db:
                    exists = db.execute(
                        select(EditArtifact.id).where(
                            EditArtifact.parent_artifact_id == parent_id,
                            EditArtifact.artifact_kind == kind,
                        )
                    ).scalar_one_or_none()
                if exists is not None:
                    continue
                subprocess.run(command, check=True, capture_output=True, timeout=120)
                local = poster if kind == "poster" else sheet
                derivative_id = uuid.uuid5(parent_id, kind)
                object_path = f"users/{creator_id}/edit-feedback/{derivative_id}/{kind}.jpg"
                storage.upload_local_file(str(local), object_path, "image/jpeg")
                metadata = storage.object_metadata(object_path)
                content_hash = metadata.md5_hash or _sha256_file(local)
                with sync_session() as db:
                    parent = db.get(EditArtifact, parent_id)
                    if parent is None or not artifact_is_eligible(db, parent):
                        storage.delete_object_generation_best_effort(
                            metadata.path,
                            generation=metadata.generation,
                        )
                        return
                    _record_derivative(
                        db,
                        parent=parent,
                        kind=kind,
                        artifact_id=derivative_id,
                        metadata=metadata,
                        content_hash=content_hash,
                    )
            except Exception:  # noqa: BLE001 - final video retention remains valid
                log.exception(
                    "edit_training_derivative_failed",
                    artifact_id=str(parent_id),
                    kind=kind,
                )


@celery_app.task(
    name="tasks.capture_edit_training_artifact",
    bind=True,
    autoretry_for=(),
    max_retries=0,
    soft_time_limit=600,
    time_limit=900,
)
def capture_edit_training_artifact(
    self,
    job_id: str,
    variant_id: str,
    render_generation_id: str,
) -> dict[str, object]:
    """Copy an eligible exact final render; never retain a raw upload."""
    from app import storage  # noqa: PLC0415

    try:
        with sync_session() as db:
            snapshot = load_render_capture_snapshot(
                db,
                job_id=job_id,
                variant_id=variant_id,
                render_generation_id=render_generation_id,
            )
        if snapshot is None:
            return {"status": "ineligible_or_stale"}
        source = storage.object_metadata(snapshot.source_path)
        source_hash = source.md5_hash
        if not source_hash:
            with tempfile.TemporaryDirectory(prefix="edit-feedback-hash-") as tmp_dir:
                exact_source = Path(tmp_dir) / "source.bin"
                storage.download_generation_to_file(
                    source.path,
                    str(exact_source),
                    generation=source.generation,
                )
                source_hash = _sha256_file(exact_source)
        with sync_session() as db:
            # Re-read immediately before minting the immutable source identity.
            current = load_render_capture_snapshot(
                db,
                job_id=job_id,
                variant_id=variant_id,
                render_generation_id=render_generation_id,
            )
            if current is None or current.source_path != source.path:
                return {"status": "ineligible_or_stale"}
            artifact, event = get_or_create_artifact(
                db,
                current,
                source_generation=source.generation,
                source_content_hash=source_hash,
                source_size_bytes=source.size,
            )
            artifact_id = artifact.id
            event_id = event.id
            destination = event.storage_path
            if event.status == "succeeded":
                return {"status": "ready", "artifact_id": str(artifact_id)}
            mark_retention_started(db, event_id)

        copied = storage.copy_object_generation(
            source.path,
            destination,
            source_generation=source.generation,
        )
        with sync_session() as db:
            artifact = db.get(EditArtifact, artifact_id)
            if artifact is None or not artifact_is_eligible(db, artifact):
                storage.delete_object_generation_best_effort(
                    copied.path,
                    generation=copied.generation,
                )
                mark_retention_failed(db, event_id, error_code="eligibility_revoked")
                return {"status": "revoked"}
            mark_retention_succeeded(
                db,
                event_id,
                generation=copied.generation,
                content_hash=copied.md5_hash or source_hash,
            )
        _build_review_derivatives(artifact_id, copied)
        return {"status": "ready", "artifact_id": str(artifact_id)}
    except Exception as exc:  # noqa: BLE001 - retention must never fail a render
        try:
            with sync_session() as db:
                if "event_id" in locals():
                    mark_retention_failed(db, event_id, error_code=type(exc).__name__)
        except Exception:  # noqa: BLE001 - best effort audit finalization
            pass
        log.exception(
            "edit_training_artifact_capture_failed",
            job_id=job_id,
            variant_id=variant_id,
        )
        return {"status": "failed", "reason": type(exc).__name__}


@celery_app.task(
    name="tasks.purge_edit_training_artifacts",
    bind=True,
    autoretry_for=(),
    max_retries=0,
    soft_time_limit=600,
    time_limit=900,
)
def purge_edit_training_artifacts(
    self,
    creator_id: str,
    consent_event_id: str | None = None,
    internal_grant_id: str | None = None,
) -> dict[str, object]:
    """Delete generation-pinned training copies, never creator product renders."""
    from app import storage  # noqa: PLC0415

    creator_uuid = uuid.UUID(creator_id)
    consent_uuid = uuid.UUID(consent_event_id) if consent_event_id else None
    internal_uuid = uuid.UUID(internal_grant_id) if internal_grant_id else None
    with sync_session() as db:
        query = select(EditArtifact).where(EditArtifact.creator_id == creator_uuid)
        if consent_uuid is not None:
            query = query.where(EditArtifact.consent_event_id == consent_uuid)
        if internal_uuid is not None:
            query = query.where(EditArtifact.internal_grant_id == internal_uuid)
        artifacts = db.execute(query).scalars().all()

    purged = 0
    failed = 0
    for artifact in artifacts:
        with sync_session() as db:
            copy_event = db.execute(
                select(TrainingArtifactRetentionEvent)
                .where(
                    TrainingArtifactRetentionEvent.artifact_id == artifact.id,
                    TrainingArtifactRetentionEvent.event_type.in_(("copy", "ready")),
                    TrainingArtifactRetentionEvent.status == "succeeded",
                )
                .order_by(TrainingArtifactRetentionEvent.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            if copy_event is None:
                continue
            purge_event = get_or_create_purge_event(db, artifact, copy_event)
            if purge_event.status == "succeeded":
                continue
            mark_retention_started(db, purge_event.id)
            purge_event_id = purge_event.id
            path = purge_event.storage_path
            generation = purge_event.storage_generation
        if storage.delete_object_generation_best_effort(path, generation=generation):
            with sync_session() as db:
                mark_retention_succeeded(
                    db,
                    purge_event_id,
                    generation=generation,
                    content_hash=purge_event.content_hash,
                )
            purged += 1
        else:
            with sync_session() as db:
                mark_retention_failed(db, purge_event_id, error_code="storage_delete_failed")
            failed += 1
    return {"status": "complete", "purged": purged, "failed": failed}
