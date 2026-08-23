"""Build consent-safe edit-learning records from append-only audit evidence."""

from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    EditArtifact,
    EditFeedbackAnnotation,
    EditInteractionReceipt,
)
from app.schemas.edit_training import CanonicalEditTrainingRecord
from app.services.edit_training_dataset import (
    DatasetCandidate,
    canonical_record,
    pseudonymous_key,
)
from app.services.training_eligibility import retained_copy_is_eligible


def _current_annotations(rows: list[EditFeedbackAnnotation]) -> list[dict[str, Any]]:
    superseded = {
        row.supersedes_annotation_id for row in rows if row.supersedes_annotation_id is not None
    }
    current = [row for row in rows if row.id not in superseded]
    current.sort(key=lambda row: (row.dimension, row.created_at, str(row.id)))
    return [
        {
            "dimension": row.dimension,
            "rating": row.rating,
            "rationale": row.rationale,
            "start_s": row.frame_start_ms / 1000 if row.frame_start_ms is not None else None,
            "end_s": row.frame_end_ms / 1000 if row.frame_end_ms is not None else None,
        }
        for row in current
    ]


def _safe_media_manifest(
    artifact: EditArtifact,
    *,
    secret: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, raw in enumerate(artifact.media_manifest or []):
        if not isinstance(raw, dict):
            continue
        analysis = raw.get("analysis") if isinstance(raw.get("analysis"), dict) else {}

        def pick(*keys: str) -> Any:
            for key in keys:
                value = raw.get(key)
                if value is None:
                    value = analysis.get(key)
                if value is not None:
                    return value
            return None

        source_identity = str(raw.get("media_id") or raw.get("media_key") or index)
        rows.append(
            {
                "media_key": pseudonymous_key(
                    secret,
                    "media",
                    f"{artifact.creator_id}:{source_identity}",
                ),
                "kind": pick("kind"),
                "duration_s": pick("duration_s"),
                "width": pick("width"),
                "height": pick("height"),
                "orientation": pick("orientation"),
                "shot_type": pick("shot_type"),
                "visual_summary": pick("visual_summary", "description", "subject"),
                "motion": pick("motion"),
                "quality": pick("quality"),
                "has_speech": pick("has_speech"),
            }
        )
    return rows


def _safe_direction_snapshot(
    snapshot: dict[str, Any] | None,
    *,
    artifact: EditArtifact,
    secret: str,
) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {}
    allowed = {
        "direction",
        "pace",
        "duration_s",
        "text_density",
        "audio_role",
        "rationale",
        "buildability_warnings",
        "provenance",
        "state",
        "goal",
        "language",
        "story_beats",
        "fast_cuts",
    }

    def sanitize(value: Any, key: str = "") -> Any:
        if key in {"media_id", "media_ref"} and isinstance(value, str):
            return pseudonymous_key(secret, "media", f"{artifact.creator_id}:{value}")
        if key in {"media_ids", "media_refs"} and isinstance(value, list):
            return [
                pseudonymous_key(secret, "media", f"{artifact.creator_id}:{item}")
                for item in value
                if isinstance(item, str)
            ]
        if isinstance(value, dict):
            return {
                str(child_key): sanitize(child, str(child_key))
                for child_key, child in value.items()
            }
        if isinstance(value, list):
            return [sanitize(child) for child in value]
        return value

    return {key: sanitize(snapshot[key], key) for key in allowed if key in snapshot}


def _safe_operation_value(
    value: Any,
    *,
    artifact: EditArtifact,
    secret: str,
    key: str = "",
) -> Any:
    denied = {
        "source_path",
        "storage_path",
        "gcs_path",
        "video_path",
        "output_url",
        "signed_url",
        "transcript",
        "raw_text",
    }
    if key.lower() in denied:
        return None
    if key.lower() in {"media_id", "clip_id"} and isinstance(value, str):
        return pseudonymous_key(secret, "media", f"{artifact.creator_id}:{value}")
    if isinstance(value, dict):
        return {
            str(child_key): _safe_operation_value(
                child,
                artifact=artifact,
                secret=secret,
                key=str(child_key),
            )
            for child_key, child in value.items()
            if str(child_key).lower() not in denied
        }
    if isinstance(value, list):
        return [_safe_operation_value(child, artifact=artifact, secret=secret) for child in value]
    return value


def _safe_operations(
    ops: list[Any] | None,
    *,
    artifact: EditArtifact,
    secret: str,
) -> list[dict[str, Any]]:
    return [
        _safe_operation_value(raw, artifact=artifact, secret=secret)
        for raw in ops or []
        if isinstance(raw, dict)
    ]


def _safe_timeline(artifact: EditArtifact, *, secret: str) -> list[dict[str, Any]]:
    receipt = artifact.render_receipt if isinstance(artifact.render_receipt, dict) else {}
    raw_rows = receipt.get("timeline") or receipt.get("moment_stages") or []
    rows: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_rows if isinstance(raw_rows, list) else []):
        if not isinstance(raw, dict):
            continue
        source_identity = str(raw.get("media_id") or raw.get("moment_id") or index)
        rows.append(
            {
                "media_key": pseudonymous_key(
                    secret,
                    "media",
                    f"{artifact.creator_id}:{source_identity}",
                ),
                "role": raw.get("role") or raw.get("kind"),
                "output_start_s": raw.get("output_start_s", raw.get("start_s")),
                "output_end_s": raw.get("output_end_s", raw.get("end_s")),
                "output_duration_s": raw.get("output_duration_s", raw.get("duration_s")),
                "transition": raw.get("transition") or raw.get("transition_after"),
            }
        )
    return rows


def _guide_record(
    artifact: EditArtifact,
    labels: list[dict[str, Any]],
    *,
    secret: str,
) -> CanonicalEditTrainingRecord:
    snapshot = _safe_direction_snapshot(
        artifact.direction_snapshot,
        artifact=artifact,
        secret=secret,
    )
    return canonical_record(
        DatasetCandidate(
            artifact_id=f"{artifact.id}:edit_guide",
            creator_id=str(artifact.creator_id),
            plan_item_id=str(artifact.plan_item_id),
            agent="edit_guide",
            media_summary=_safe_media_manifest(artifact, secret=secret),
            user_intent=str(snapshot.get("goal") or ""),
            proposed=snapshot,
            execution={
                "rendered": True,
                "direction_provenance": snapshot.get("provenance"),
            },
            labels=labels,
            versions={
                "artifact_schema": artifact.artifact_schema_version,
                "prompt": artifact.prompt_version or "unknown",
                "model": artifact.effective_model or artifact.requested_model or "unknown",
            },
            lineage_parts=(
                str(artifact.plan_item_id),
                artifact.media_digest or "unknown-media",
                "edit_guide",
            ),
        ),
        secret=secret,
    )


def _proposal_record(
    artifact: EditArtifact,
    labels: list[dict[str, Any]],
    *,
    secret: str,
) -> CanonicalEditTrainingRecord:
    snapshot = _safe_direction_snapshot(
        artifact.direction_snapshot,
        artifact=artifact,
        secret=secret,
    )
    return canonical_record(
        DatasetCandidate(
            artifact_id=f"{artifact.id}:edit_proposal",
            creator_id=str(artifact.creator_id),
            plan_item_id=str(artifact.plan_item_id),
            agent="edit_proposal",
            media_summary=_safe_media_manifest(artifact, secret=secret),
            user_intent=str(snapshot.get("goal") or ""),
            proposed={"direction": snapshot, "timeline": _safe_timeline(artifact, secret=secret)},
            execution={"rendered": True, "render_hash": artifact.render_hash},
            labels=labels,
            versions={
                "artifact_schema": artifact.artifact_schema_version,
                "prompt": artifact.prompt_version or "unknown",
                "model": artifact.effective_model or artifact.requested_model or "unknown",
            },
            lineage_parts=(
                str(artifact.plan_item_id),
                artifact.media_digest or "unknown-media",
                "edit_proposal",
            ),
        ),
        secret=secret,
    )


def _copilot_record(
    artifact: EditArtifact,
    receipt: EditInteractionReceipt,
    labels: list[dict[str, Any]],
    *,
    secret: str,
) -> CanonicalEditTrainingRecord:
    return canonical_record(
        DatasetCandidate(
            artifact_id=f"{artifact.id}:edit_copilot:{receipt.id}",
            creator_id=str(artifact.creator_id),
            plan_item_id=str(artifact.plan_item_id),
            agent="edit_copilot",
            media_summary=_safe_media_manifest(artifact, secret=secret),
            user_intent=receipt.utterance,
            proposed={
                "intent": receipt.inferred_intent,
                "reply": receipt.model_reply,
                "operations": _safe_operations(
                    receipt.proposed_operations,
                    artifact=artifact,
                    secret=secret,
                ),
                "proposal_outcome": receipt.proposal_outcome,
            },
            execution={
                "outcome": receipt.execution_outcome,
                "rejection_reasons": receipt.rejection_reasons,
                "before_revision_hash": receipt.before_revision_hash,
                "after_revision_hash": receipt.after_revision_hash,
            },
            labels=labels,
            versions={
                "artifact_schema": artifact.artifact_schema_version,
                "prompt": receipt.prompt_version,
                "model": receipt.model,
            },
            lineage_parts=(
                str(artifact.plan_item_id),
                artifact.media_digest or "unknown-media",
                receipt.inferred_intent,
            ),
        ),
        secret=secret,
    )


def _receipt_matches_artifact_grant(
    receipt: EditInteractionReceipt,
    artifact: EditArtifact,
) -> bool:
    if receipt.eligibility_basis != artifact.eligibility_basis:
        return False
    if artifact.eligibility_basis == "training_consent":
        return bool(
            artifact.consent_event_id
            and receipt.consent_event_id == artifact.consent_event_id
            and receipt.internal_grant_id is None
        )
    return bool(
        artifact.internal_grant_id
        and receipt.internal_grant_id == artifact.internal_grant_id
        and receipt.consent_event_id is None
    )


def build_training_records(
    db: Session,
    *,
    secret: str,
) -> tuple[list[CanonicalEditTrainingRecord], set[uuid.UUID]]:
    """Project eligible retained artifacts; never serialize whole ORM/JSONB rows."""
    artifacts = (
        db.execute(
            select(EditArtifact)
            .where(EditArtifact.artifact_kind == "final_render")
            .order_by(EditArtifact.created_at)
        )
        .scalars()
        .all()
    )
    artifact_ids = {row.id for row in artifacts}
    annotations = (
        db.execute(
            select(EditFeedbackAnnotation).where(
                EditFeedbackAnnotation.artifact_id.in_(artifact_ids)
            )
        )
        .scalars()
        .all()
        if artifact_ids
        else []
    )
    executions = (
        db.execute(
            select(EditInteractionReceipt).where(EditInteractionReceipt.event_kind == "save_link")
        )
        .scalars()
        .all()
        if artifact_ids
        else []
    )

    annotations_by_artifact: dict[uuid.UUID, list[EditFeedbackAnnotation]] = defaultdict(list)
    executions_by_job_variant: dict[tuple[uuid.UUID | None, str], list[EditInteractionReceipt]] = (
        defaultdict(list)
    )
    for row in annotations:
        annotations_by_artifact[row.artifact_id].append(row)
    for row in executions:
        if row.event_kind == "save_link":
            executions_by_job_variant[(row.job_id, row.variant_id)].append(row)

    records: list[CanonicalEditTrainingRecord] = []
    included: set[uuid.UUID] = set()
    for artifact in artifacts:
        if not retained_copy_is_eligible(db, artifact):
            continue
        labels = _current_annotations(annotations_by_artifact[artifact.id])
        records.append(_guide_record(artifact, labels, secret=secret))
        records.append(_proposal_record(artifact, labels, secret=secret))
        revision_hash = (
            artifact.render_receipt.get("revision_hash")
            if isinstance(artifact.render_receipt, dict)
            else None
        )
        matching = [
            row
            for row in executions_by_job_variant[(artifact.job_id, artifact.variant_id or "")]
            if revision_hash
            and row.after_revision_hash == revision_hash
            and _receipt_matches_artifact_grant(row, artifact)
        ]
        for receipt in matching:
            records.append(_copilot_record(artifact, receipt, labels, secret=secret))
        included.add(artifact.id)
    return records, included
