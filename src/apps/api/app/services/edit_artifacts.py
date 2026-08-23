"""Render-time identity and retention-event helpers for edit-feedback artifacts.

This module deliberately keeps all database work short-lived.  GCS metadata,
copy, and deletion are performed by :mod:`app.tasks.edit_training_artifacts`
after the snapshot transaction has committed, so a slow storage operation never
holds a Job/PlanItem row lock.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import EditArtifact, Job, PlanItem, TrainingArtifactRetentionEvent
from app.services.edit_training_dataset import dataset_split
from app.services.training_eligibility import TrainingEligibility, resolve_training_eligibility

FINAL_RENDER_KIND = "final_render"
COPY_EVENT = "copy"
PURGE_EVENT = "purge"
COPY_STATUS_PENDING = "pending"
COPY_STATUS_STARTED = "started"
COPY_STATUS_SUCCEEDED = "succeeded"
COPY_STATUS_FAILED = "failed"


@dataclass(frozen=True)
class RenderCaptureSnapshot:
    """Everything required to capture one current, ready final variant."""

    job_id: uuid.UUID
    creator_id: uuid.UUID
    plan_item_id: uuid.UUID
    variant_id: str
    render_generation_id: str
    source_path: str
    artifact_kind: str
    eligibility: TrainingEligibility
    direction_snapshot: dict[str, Any]
    media_manifest: list[dict[str, Any]]
    render_receipt: dict[str, Any]
    proposal_version: str | None
    media_digest: str | None
    prompt_version: str | None
    effective_model: str | None
    duration_ms: int | None
    width: int | None
    height: int | None
    content_type: str
    render_hash: str
    render_receipt_hash: str


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _approved_snapshot(item: PlanItem) -> dict[str, Any]:
    raw = item.edit_proposal if isinstance(item.edit_proposal, dict) else {}
    approved = raw.get("last_approved") or raw.get("approved") or raw
    if not isinstance(approved, dict):
        return {}
    snapshot = approved.get("snapshot") or approved.get("direction_snapshot") or approved
    if not isinstance(snapshot, dict):
        return {}
    # Direction snapshots are an allowlisted audit summary.  In particular,
    # never copy mutable proposal envelopes or source storage paths wholesale.
    allowed = (
        "direction",
        "goal",
        "pace",
        "duration_s",
        "title",
        "text_density",
        "audio_role",
        "rationale",
        "buildability_warnings",
        "provenance",
        "state",
        "language",
        "story_beats",
        "fast_cuts",
        "output_orientation",
        "output_orientation_reason",
    )
    return {key: snapshot[key] for key in allowed if key in snapshot}


def _media_manifest(item: PlanItem) -> list[dict[str, Any]]:
    raw = item.edit_proposal if isinstance(item.edit_proposal, dict) else {}
    approved = raw.get("last_approved") or raw.get("approved") or raw
    snapshot = approved.get("snapshot") if isinstance(approved, dict) else None
    media = snapshot.get("media") if isinstance(snapshot, dict) else None
    if not isinstance(media, list):
        return []
    allowed = (
        "media_id",
        "kind",
        "duration_s",
        "aspect",
        "content_hash",
        "analysis",
    )
    manifest: list[dict[str, Any]] = []
    for row in media:
        if not isinstance(row, dict):
            continue
        manifest.append({key: row[key] for key in allowed if key in row})
    return manifest


def _variant(job: Job, variant_id: str) -> dict[str, Any] | None:
    rows = (job.assembly_plan or {}).get("variants") or []
    return next(
        (row for row in rows if isinstance(row, dict) and row.get("variant_id") == variant_id),
        None,
    )


def _is_raw_upload(path: str, creator_id: uuid.UUID) -> bool:
    normalized = path.lstrip("/")
    creator = str(creator_id)
    denied_prefixes = (
        f"users/{creator}/plan/",
        f"users/{creator}/plan-pool/",
        f"users/{creator}/uploads/",
        "dev-user/",
        "raw-uploads/",
        "uploads/",
    )
    return (
        normalized.startswith(denied_prefixes)
        or "/raw/" in normalized
        or "/sources/" in normalized
        or normalized.endswith("/raw.mp4")
    )


def _probe_dimensions(job: Job, variant: dict[str, Any]) -> tuple[int | None, int | None]:
    width = variant.get("width")
    height = variant.get("height")
    metadata = job.probe_metadata if isinstance(job.probe_metadata, dict) else {}
    width = width if isinstance(width, int) else metadata.get("width")
    height = height if isinstance(height, int) else metadata.get("height")
    return (width if isinstance(width, int) else None, height if isinstance(height, int) else None)


def load_render_capture_snapshot(
    db: Session,
    *,
    job_id: str | uuid.UUID,
    variant_id: str,
    render_generation_id: str | None = None,
) -> RenderCaptureSnapshot | None:
    """Read a ready final variant and re-check current training eligibility.

    This is intentionally a non-locking read.  The caller must invoke it again
    immediately before the external copy to close the revoke/stale-render race.
    """

    job = db.get(Job, uuid.UUID(str(job_id)))
    if job is None or job.content_plan_item_id is None:
        return None
    item = db.get(PlanItem, job.content_plan_item_id)
    if item is None:
        return None
    variant = _variant(job, variant_id)
    if variant is None or variant.get("render_status") != "ready" or not variant.get("ok", True):
        return None
    source_path = variant.get("video_path")
    generation = variant.get("render_generation_id")
    if (
        not isinstance(source_path, str)
        or not source_path.strip()
        or _is_raw_upload(source_path, job.user_id)
    ):
        return None
    if not isinstance(generation, str) or not generation.strip():
        return None
    if render_generation_id is not None and generation != render_generation_id:
        return None

    eligibility = resolve_training_eligibility(db, job.user_id)
    if not eligibility.eligible:
        return None
    receipt = variant.get("render_receipt")
    if not isinstance(receipt, dict):
        receipt = {
            "schema_version": "edit-artifact-v1",
            "variant_id": variant_id,
            "render_generation_id": generation,
            "render_status": "ready",
        }
    else:
        receipt = dict(receipt)
    if not receipt.get("revision_hash"):
        guided_revision = variant.get("guided_edit_revision")
        receipt["revision_hash"] = (
            guided_revision.get("state_hash")
            if isinstance(guided_revision, dict) and guided_revision.get("state_hash")
            else generation
        )
    receipt_hash = str(variant.get("render_receipt_hash") or _canonical_hash(receipt))
    render_hash = str(variant.get("render_hash") or receipt_hash)
    duration_s = variant.get("duration_s")
    if not isinstance(duration_s, (int, float)):
        duration_s = receipt.get("duration_s") or receipt.get("target_duration_s")
    if not isinstance(duration_s, (int, float)):
        timeline = receipt.get("timeline") or receipt.get("moment_stages") or []
        if isinstance(timeline, list):
            duration_s = max(
                (
                    float(row.get("output_end_s") or row.get("end_s") or 0)
                    for row in timeline
                    if isinstance(row, dict)
                ),
                default=0.0,
            )
    if not isinstance(duration_s, (int, float)) or duration_s <= 0:
        duration_s = (
            (job.probe_metadata or {}).get("duration_s")
            if isinstance(job.probe_metadata, dict)
            else None
        )
    duration_ms = round(float(duration_s) * 1000) if isinstance(duration_s, (int, float)) else None
    width, height = _probe_dimensions(job, variant)
    return RenderCaptureSnapshot(
        job_id=job.id,
        creator_id=job.user_id,
        plan_item_id=item.id,
        variant_id=variant_id,
        render_generation_id=generation,
        source_path=source_path,
        artifact_kind=FINAL_RENDER_KIND,
        eligibility=eligibility,
        direction_snapshot=_approved_snapshot(item),
        media_manifest=_media_manifest(item),
        render_receipt=receipt,
        proposal_version=(
            str(variant.get("proposal_version"))
            if variant.get("proposal_version") is not None
            else None
        ),
        media_digest=(
            str(variant.get("media_digest")) if variant.get("media_digest") is not None else None
        ),
        prompt_version=(
            str(variant.get("prompt_version"))
            if variant.get("prompt_version") is not None
            else None
        ),
        effective_model=(
            str(variant.get("effective_model"))
            if variant.get("effective_model") is not None
            else None
        ),
        duration_ms=duration_ms,
        width=width,
        height=height,
        content_type=str(variant.get("content_type") or "video/mp4"),
        render_hash=render_hash,
        render_receipt_hash=receipt_hash,
    )


def retention_copy_path(creator_id: uuid.UUID, artifact_id: uuid.UUID) -> str:
    return f"users/{creator_id}/edit-feedback/{artifact_id}/final.mp4"


def split_for_capture(creator_id: uuid.UUID, plan_item_id: uuid.UUID) -> tuple[str, str]:
    secret = settings.training_dataset_split_secret
    creator_split = dataset_split(secret, str(creator_id))
    # Creator isolation is the stronger boundary; assigning every item from
    # that creator to the same split also guarantees plan-item isolation.
    return creator_split, creator_split


def get_or_create_artifact(
    db: Session,
    snapshot: RenderCaptureSnapshot,
    *,
    source_generation: str,
    source_content_hash: str,
    source_size_bytes: int | None,
) -> tuple[EditArtifact, TrainingArtifactRetentionEvent]:
    """Commit the identity row and pending copy event, without touching GCS."""

    existing = db.execute(
        select(EditArtifact)
        .where(
            EditArtifact.job_id == snapshot.job_id,
            EditArtifact.variant_id == snapshot.variant_id,
            EditArtifact.render_generation_id == snapshot.render_generation_id,
            EditArtifact.artifact_kind == FINAL_RENDER_KIND,
        )
        .order_by(EditArtifact.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if existing is None:
        parent = db.execute(
            select(EditArtifact)
            .where(
                EditArtifact.job_id == snapshot.job_id,
                EditArtifact.variant_id == snapshot.variant_id,
                EditArtifact.artifact_kind == FINAL_RENDER_KIND,
            )
            .order_by(EditArtifact.created_at.desc(), EditArtifact.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        creator_split, plan_split = split_for_capture(snapshot.creator_id, snapshot.plan_item_id)
        existing = EditArtifact(
            id=uuid.uuid4(),
            creator_id=snapshot.creator_id,
            plan_item_id=snapshot.plan_item_id,
            job_id=snapshot.job_id,
            parent_artifact_id=parent.id if parent is not None else None,
            variant_id=snapshot.variant_id,
            render_generation_id=snapshot.render_generation_id,
            artifact_kind=FINAL_RENDER_KIND,
            proposal_version=snapshot.proposal_version,
            media_digest=snapshot.media_digest,
            direction_snapshot=snapshot.direction_snapshot,
            # Exact immutable object content identity from storage metadata;
            # the separately hashed receipt is not a substitute for video bytes.
            render_hash=source_content_hash,
            render_receipt_hash=snapshot.render_receipt_hash,
            render_receipt_schema_version=str(snapshot.render_receipt.get("schema_version") or "1"),
            render_receipt=snapshot.render_receipt,
            prompt_version=snapshot.prompt_version,
            effective_model=snapshot.effective_model,
            media_manifest=snapshot.media_manifest,
            storage_path=snapshot.source_path,
            storage_generation=source_generation,
            storage_content_hash=source_content_hash,
            storage_size_bytes=source_size_bytes,
            content_type=snapshot.content_type,
            width=snapshot.width,
            height=snapshot.height,
            duration_ms=snapshot.duration_ms,
            capture_origin=(
                "internal" if snapshot.eligibility.basis == "internal_grant" else "creator"
            ),
            eligibility_basis=snapshot.eligibility.basis or "training_consent",
            consent_event_id=snapshot.eligibility.consent_event_id,
            internal_grant_id=snapshot.eligibility.internal_grant_id,
            creator_split=creator_split,
            plan_item_split=plan_split,
        )
        db.add(existing)
        db.flush()

    copy_key = f"copy:{existing.id}:{existing.storage_generation}"
    event = db.execute(
        select(TrainingArtifactRetentionEvent).where(
            TrainingArtifactRetentionEvent.artifact_id == existing.id,
            TrainingArtifactRetentionEvent.idempotency_key == copy_key,
        )
    ).scalar_one_or_none()
    if event is None:
        event = TrainingArtifactRetentionEvent(
            id=uuid.uuid4(),
            creator_id=existing.creator_id,
            artifact_id=existing.id,
            event_type=COPY_EVENT,
            status=COPY_STATUS_PENDING,
            storage_path=retention_copy_path(existing.creator_id, existing.id),
            storage_generation="pending",
            idempotency_key=copy_key,
        )
        db.add(event)
    db.commit()
    db.refresh(existing)
    db.refresh(event)
    return existing, event


def mark_retention_started(db: Session, event_id: uuid.UUID) -> None:
    event = db.get(TrainingArtifactRetentionEvent, event_id)
    if event is None:
        return
    event.status = COPY_STATUS_STARTED
    db.commit()


def mark_retention_succeeded(
    db: Session,
    event_id: uuid.UUID,
    *,
    generation: str,
    content_hash: str | None,
    completed_at: datetime | None = None,
) -> None:
    event = db.get(TrainingArtifactRetentionEvent, event_id)
    if event is None:
        return
    event.status = COPY_STATUS_SUCCEEDED
    event.storage_generation = generation
    event.content_hash = content_hash
    event.completed_at = completed_at or datetime.now(UTC)
    event.error_code = None
    db.commit()


def mark_retention_failed(db: Session, event_id: uuid.UUID, *, error_code: str) -> None:
    event = db.get(TrainingArtifactRetentionEvent, event_id)
    if event is None:
        return
    event.status = COPY_STATUS_FAILED
    event.error_code = error_code[:200]
    event.completed_at = datetime.now(UTC)
    db.commit()


def get_or_create_purge_event(
    db: Session, artifact: EditArtifact, copy_event: TrainingArtifactRetentionEvent
) -> TrainingArtifactRetentionEvent:
    key = f"purge:{copy_event.id}:{copy_event.storage_generation}"
    event = db.execute(
        select(TrainingArtifactRetentionEvent).where(
            TrainingArtifactRetentionEvent.artifact_id == artifact.id,
            TrainingArtifactRetentionEvent.idempotency_key == key,
        )
    ).scalar_one_or_none()
    if event is None:
        event = TrainingArtifactRetentionEvent(
            id=uuid.uuid4(),
            creator_id=artifact.creator_id,
            artifact_id=artifact.id,
            event_type=PURGE_EVENT,
            status=COPY_STATUS_PENDING,
            storage_path=copy_event.storage_path,
            storage_generation=copy_event.storage_generation,
            content_hash=copy_event.content_hash,
            idempotency_key=key,
        )
        db.add(event)
        db.commit()
        db.refresh(event)
    return event
