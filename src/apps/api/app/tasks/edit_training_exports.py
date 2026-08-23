"""Asynchronous consent-safe training dataset exports."""

from __future__ import annotations

import hashlib
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import structlog

from app.config import settings
from app.database import sync_session
from app.models import EditArtifact, TrainingDatasetExport
from app.services.edit_training_dataset import records_to_jsonl, records_to_parquet
from app.services.edit_training_exports import build_training_records
from app.services.training_eligibility import retained_copy_is_eligible
from app.worker import celery_app

log = structlog.get_logger()


def _mark_failed(export_id: uuid.UUID, reason: str) -> None:
    with sync_session() as db:
        row = db.get(TrainingDatasetExport, export_id, with_for_update=True)
        if row is None or row.status in {"ready", "revoked"}:
            return
        row.status = "failed"
        row.failure_reason = reason[:500]
        db.commit()


@celery_app.task(
    name="tasks.build_edit_training_export",
    bind=True,
    autoretry_for=(),
    max_retries=0,
    soft_time_limit=900,
    time_limit=1200,
)
def build_edit_training_export(self, export_id: str) -> dict[str, object]:
    """Build one private short-lived file; re-check every artifact before publish."""
    export_uuid = uuid.UUID(export_id)
    uploaded = None
    try:
        with sync_session() as db:
            row = db.get(TrainingDatasetExport, export_uuid, with_for_update=True)
            if row is None:
                return {"status": "missing"}
            if row.status == "ready":
                return {"status": "ready", "row_count": row.row_count or 0}
            if row.status == "revoked":
                return {"status": "revoked"}
            row.status = "building"
            db.commit()

        with sync_session() as db:
            records, artifact_ids = build_training_records(
                db,
                secret=settings.training_dataset_split_secret,
            )
            artifacts = {
                artifact.id: artifact
                for artifact in db.query(EditArtifact).filter(EditArtifact.id.in_(artifact_ids))
            }
            manifest = {
                "schema_version": 1,
                "artifact_ids": sorted(str(value) for value in artifact_ids),
                "artifact_hashes": {
                    str(value): artifacts[value].render_hash
                    for value in sorted(artifact_ids, key=str)
                },
                "consent_event_ids": sorted(
                    {
                        str(artifact.consent_event_id)
                        for artifact in artifacts.values()
                        if artifact.consent_event_id is not None
                    }
                ),
                "internal_grant_ids": sorted(
                    {
                        str(artifact.internal_grant_id)
                        for artifact in artifacts.values()
                        if artifact.internal_grant_id is not None
                    }
                ),
                "creator_split_version": "creator-hmac-v1",
                "plan_item_split_version": "creator-hmac-v1",
                "record_count": len(records),
            }

        with sync_session() as db:
            row = db.get(TrainingDatasetExport, export_uuid)
            if row is None:
                return {"status": "missing"}
            export_format = row.export_format

        suffix = ".jsonl" if export_format == "jsonl" else ".parquet"
        content_type = (
            "application/x-ndjson" if export_format == "jsonl" else "application/vnd.apache.parquet"
        )
        with tempfile.TemporaryDirectory(prefix="edit-training-export-") as tmp_dir:
            local_path = Path(tmp_dir) / f"dataset{suffix}"
            if export_format == "jsonl":
                local_path.write_bytes(records_to_jsonl(records))
            else:
                records_to_parquet(records, str(local_path))
            content_hash = hashlib.sha256(local_path.read_bytes()).hexdigest()
            object_path = f"training-exports/{export_uuid}{suffix}"
            from app import storage  # noqa: PLC0415

            storage.upload_local_file(str(local_path), object_path, content_type)
            uploaded = storage.object_metadata(object_path)

        # The upload is not publishable until every artifact is still eligible.
        with sync_session() as db:
            current = {
                artifact.id: artifact
                for artifact in db.query(EditArtifact).filter(EditArtifact.id.in_(artifact_ids))
            }
            still_safe = set(current) == artifact_ids and all(
                retained_copy_is_eligible(db, artifact) for artifact in current.values()
            )
            row = db.get(TrainingDatasetExport, export_uuid, with_for_update=True)
            if row is None:
                still_safe = False
            elif not still_safe:
                row.status = "revoked"
                row.failure_reason = "eligibility_changed_during_export"
                db.commit()
            else:
                row.status = "ready"
                row.row_count = len(records)
                row.content_hash = content_hash
                row.manifest = manifest
                row.storage_path = uploaded.path
                row.storage_generation = uploaded.generation
                row.expires_at = datetime.now(UTC) + timedelta(
                    hours=settings.training_export_ttl_hours
                )
                db.commit()

        if not still_safe:
            from app import storage  # noqa: PLC0415

            storage.delete_object_generation_best_effort(
                uploaded.path,
                generation=uploaded.generation,
            )
            return {"status": "revoked"}
        return {"status": "ready", "row_count": len(records)}
    except Exception as exc:  # noqa: BLE001 - durable status records the failure
        if uploaded is not None:
            from app import storage  # noqa: PLC0415

            storage.delete_object_generation_best_effort(
                uploaded.path,
                generation=uploaded.generation,
            )
        _mark_failed(export_uuid, type(exc).__name__)
        log.exception("edit_training_export_failed", export_id=export_id)
        return {"status": "failed", "reason": type(exc).__name__}
