"""Admin review and consent-safe export endpoints for edit-learning artifacts."""

from __future__ import annotations

import base64
import json
import uuid
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import and_, or_, select

from app.routes.admin import _require_admin
from app.services.edit_training_dataset import REQUIRED_REVIEW_DIMENSIONS

router = APIRouter()

ADMIN_PLAYBACK_TTL_MIN = 15
_RATING = Literal["good", "bad", "mixed", "not_applicable"]


class TimelineEventOut(BaseModel):
    id: str
    kind: str
    label: str | None = None
    start_s: float = Field(ge=0)
    end_s: float | None = Field(default=None, ge=0)
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class ArtifactOut(BaseModel):
    id: str
    artifact_id: str
    title: str | None = None
    creator_group_id: str | None = None
    plan_item_id: str
    job_id: str | None = None
    variant_id: str | None = None
    format: str | None = None
    language: str | None = None
    media_mix: str | None = None
    prompt_version: str | None = None
    model_version: str | None = None
    created_at: str
    duration_s: float = Field(ge=0)
    render_generation: str
    render_receipt: dict[str, Any] | None = None
    review_state: Literal["unreviewed", "reviewed", "needs_correction"]
    quality_signal: _RATING | None = None
    edit_signal: _RATING | None = None
    reviewed_at: str | None = None
    edit_count: int = Field(default=0, ge=0)
    poster_url: str | None = None
    playback_url: str | None = None
    playback_identity: str | None = None
    playback_expires_at: str | None = None
    timeline: list[TimelineEventOut] = Field(default_factory=list)


class AnnotationOut(BaseModel):
    id: str
    dimension: str
    rating: _RATING
    rationale: str | None = None
    frame_start_s: float | None = None
    frame_end_s: float | None = None
    reviewer: str | None = None
    created_at: str
    superseded_by: str | None = None
    is_current: bool
    current: bool


class ListArtifactsResponse(BaseModel):
    items: list[ArtifactOut]
    next_cursor: str | None = None
    total: int


class ArtifactDetailResponse(BaseModel):
    artifact: ArtifactOut
    annotations: list[AnnotationOut]
    timeline: list[TimelineEventOut]
    proposal: dict[str, Any] | None = None
    execution_receipt: dict[str, Any] | None = None


class SaveAnnotationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dimension: str
    rating: _RATING
    rationale: str | None = Field(default=None, max_length=4000)
    frame_start_s: float | None = Field(default=None, ge=0)
    frame_end_s: float | None = Field(default=None, ge=0)
    supersedes_annotation_id: str | None = None

    @model_validator(mode="after")
    def validate_annotation(self) -> SaveAnnotationRequest:
        if self.dimension not in REQUIRED_REVIEW_DIMENSIONS:
            raise ValueError("unknown review dimension")
        if self.rating != "not_applicable" and not (self.rationale or "").strip():
            raise ValueError("rationale is required for substantive ratings")
        if (self.frame_start_s is None) != (self.frame_end_s is None):
            raise ValueError("frame range requires both start and end")
        if self.frame_start_s is not None and self.frame_end_s <= self.frame_start_s:
            raise ValueError("frame range end must be after start")
        return self


class SaveAnnotationResponse(BaseModel):
    annotation: AnnotationOut


class SaveAnnotationsBulkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    annotations: list[SaveAnnotationRequest] = Field(min_length=1, max_length=15)

    @model_validator(mode="after")
    def validate_unique_dimensions(self) -> SaveAnnotationsBulkRequest:
        dimensions = [annotation.dimension for annotation in self.annotations]
        if len(dimensions) != len(set(dimensions)):
            raise ValueError("bulk annotations must use unique dimensions")
        return self


class SaveAnnotationsBulkResponse(BaseModel):
    annotations: list[AnnotationOut]


class CreateExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    export_format: Literal["jsonl", "parquet"] = "jsonl"
    idempotency_key: str = Field(min_length=1, max_length=128)


class ExportResponse(BaseModel):
    id: str
    status: Literal["pending", "building", "ready", "failed", "revoked"]
    export_format: Literal["jsonl", "parquet"]
    row_count: int | None = None
    content_hash: str | None = None
    failure_reason: str | None = None
    expires_at: str | None = None
    download_url: str | None = None


class DatasetReadinessResponse(BaseModel):
    ready: bool
    reviewed_artifacts: int
    creator_groups: int
    missing_dimensions: dict[str, int]
    blockers: list[str]


class InternalAccountRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["grant", "revoke"]
    idempotency_key: str = Field(min_length=1, max_length=128)
    reason: str | None = Field(default=None, max_length=500)


class InternalAccountResponse(BaseModel):
    creator_id: str
    active: bool
    grant_id: str | None = None


def _cursor(created_at: datetime, artifact_id: uuid.UUID) -> str:
    raw = json.dumps([created_at.isoformat(), str(artifact_id)], separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(value: str) -> tuple[datetime, uuid.UUID]:
    try:
        padded = value + "=" * (-len(value) % 4)
        created_raw, artifact_raw = json.loads(base64.urlsafe_b64decode(padded).decode())
        created_at = datetime.fromisoformat(created_raw)
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        return created_at, uuid.UUID(artifact_raw)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail="invalid cursor") from exc


def _stratified_cursor(offset: int) -> str:
    raw = json.dumps(["stratified", offset], separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_stratified_cursor(value: str | None) -> int:
    if not value:
        return 0
    try:
        padded = value + "=" * (-len(value) % 4)
        mode, offset = json.loads(base64.urlsafe_b64decode(padded).decode())
        if mode != "stratified" or not isinstance(offset, int) or offset < 0:
            raise ValueError
        return offset
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail="invalid stratified cursor") from exc


def _next_chronological_cursor(raw_rows: list[Any], limit: int) -> str | None:
    if len(raw_rows) <= limit or limit < 1:
        return None
    artifact = raw_rows[limit - 1][0]
    return _cursor(artifact.created_at, artifact.id)


def _current_annotations(rows: list[Any]) -> tuple[dict[str, Any], dict[uuid.UUID, uuid.UUID]]:
    superseded_by = {
        row.supersedes_annotation_id: row.id
        for row in rows
        if row.supersedes_annotation_id is not None
    }
    current: dict[str, Any] = {}
    for row in sorted(rows, key=lambda item: (item.created_at, str(item.id))):
        if row.id not in superseded_by:
            current[row.dimension] = row
    return current, superseded_by


def _review_state(current: dict[str, Any]) -> str:
    if not current:
        return "unreviewed"
    if REQUIRED_REVIEW_DIMENSIONS.issubset(current):
        return "reviewed"
    return "needs_correction"


def _stratified_review_order(
    rows: list[tuple[Any, str | None, ArtifactOut]],
) -> list[tuple[Any, str | None, ArtifactOut]]:
    """Round-robin recent renders across format and model strata.

    Parent artifacts distinguish first-pass outputs from versions that were
    subsequently edited. Missing metadata is its own explicit stratum rather
    than silently dropping an artifact from the queue.
    """

    buckets: dict[tuple[str, ...], deque[tuple[Any, str | None, ArtifactOut]]] = defaultdict(deque)
    for row in rows:
        artifact, _format, payload = row
        key = (
            payload.format or "unknown-format",
            payload.language or "unknown-language",
            payload.media_mix or "unknown-media-mix",
            payload.prompt_version or "unknown-prompt",
            payload.model_version or "unknown-model",
            (
                "heavily-edited"
                if payload.edit_count >= 2
                else "edited"
                if payload.edit_count == 1
                else "first-pass"
            ),
        )
        buckets[key].append(row)

    ordered: list[tuple[Any, str | None, ArtifactOut]] = []
    while buckets:
        for key in sorted(tuple(buckets)):
            bucket = buckets[key]
            ordered.append(bucket.popleft())
            if not bucket:
                del buckets[key]
    return ordered


def _media_mix(manifest: list[dict[str, Any]] | None) -> str | None:
    kinds = {str(row.get("kind")) for row in manifest or [] if row.get("kind")}
    if not kinds:
        return None
    if len(kinds) > 1:
        return "mixed"
    return next(iter(kinds))


def _language(artifact: Any) -> str | None:
    snapshot = getattr(artifact, "direction_snapshot", None)
    if isinstance(snapshot, dict) and isinstance(snapshot.get("language"), str):
        return snapshot["language"]
    return None


def _creator_group_id(artifact: Any) -> str | None:
    from app.config import settings  # noqa: PLC0415
    from app.services.edit_training_dataset import pseudonymous_key  # noqa: PLC0415

    if len(settings.training_dataset_split_secret) < 16:
        return None
    return pseudonymous_key(
        settings.training_dataset_split_secret,
        "creator",
        str(artifact.creator_id),
    )


def _timeline(receipt: dict[str, Any] | None, duration_s: float) -> list[TimelineEventOut]:
    if not isinstance(receipt, dict):
        return []
    rows = receipt.get("timeline") or receipt.get("moment_stages") or []
    if not isinstance(rows, list):
        return []
    events: list[TimelineEventOut] = []
    cursor = 0.0
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        start = row.get("output_start_s", row.get("start_s", cursor))
        end = row.get("output_end_s", row.get("end_s"))
        try:
            start_s = max(0.0, float(start))
            end_s = float(end) if end is not None else None
        except (TypeError, ValueError):
            continue
        if end_s is None:
            raw_duration = row.get("output_duration_s", row.get("duration_s", 0))
            try:
                end_s = start_s + max(0.0, float(raw_duration))
            except (TypeError, ValueError):
                end_s = start_s
        end_s = min(max(start_s, end_s), duration_s)
        events.append(
            TimelineEventOut(
                id=str(row.get("moment_id") or row.get("id") or f"segment-{index}"),
                kind=str(row.get("role") or row.get("kind") or "cut"),
                label=str(row.get("label") or row.get("media_id") or f"Cut {index + 1}"),
                start_s=start_s,
                end_s=end_s,
            )
        )
        cursor = end_s
    return events


def _playback(artifact: Any, retention_rows: list[Any]) -> tuple[str | None, str | None]:
    ready = [
        row
        for row in retention_rows
        if row.event_type in {"copy", "ready"} and row.status == "succeeded"
    ]
    if not ready:
        return None, None
    row = max(ready, key=lambda item: (item.completed_at or item.created_at, str(item.id)))
    try:
        from app.storage import signed_get_url  # noqa: PLC0415

        return (
            signed_get_url(row.storage_path, expiration_minutes=ADMIN_PLAYBACK_TTL_MIN),
            f"{row.storage_path}:{row.storage_generation}",
        )
    except Exception:
        return None, f"{row.storage_path}:{row.storage_generation}"


def _annotation_out(row: Any, superseded_by: dict[uuid.UUID, uuid.UUID]) -> AnnotationOut:
    superseder = superseded_by.get(row.id)
    return AnnotationOut(
        id=str(row.id),
        dimension=row.dimension,
        rating=row.rating,
        rationale=row.rationale,
        frame_start_s=(row.frame_start_ms / 1000 if row.frame_start_ms is not None else None),
        frame_end_s=(row.frame_end_ms / 1000 if row.frame_end_ms is not None else None),
        reviewer=row.reviewer_identity,
        created_at=row.created_at.isoformat(),
        superseded_by=str(superseder) if superseder else None,
        is_current=superseder is None,
        current=superseder is None,
    )


def _artifact_out(
    artifact: Any,
    *,
    edit_format: str | None,
    annotations: list[Any],
    retention_rows: list[Any],
    include_receipt: bool,
    poster_url: str | None = None,
) -> ArtifactOut:
    current, _ = _current_annotations(annotations)
    duration_s = max(0.0, float(artifact.duration_ms or 0) / 1000)
    playback_url, playback_identity = _playback(artifact, retention_rows)
    reviewed_at = max((row.created_at for row in current.values()), default=None)
    timeline = _timeline(artifact.render_receipt, duration_s)
    receipt_projection = None
    edit_count = 0
    raw_receipt = artifact.render_receipt if isinstance(artifact.render_receipt, dict) else {}
    revision_number = raw_receipt.get("revision_number")
    if isinstance(revision_number, int):
        edit_count = max(0, revision_number - 1)
    if include_receipt:
        receipt_projection = {
            "schema_version": raw_receipt.get("schema_version"),
            "revision_hash": raw_receipt.get("revision_hash"),
            "timeline": [event.model_dump(mode="json") for event in timeline],
        }
    return ArtifactOut(
        id=str(artifact.id),
        artifact_id=str(artifact.id),
        title=(getattr(artifact, "direction_snapshot", None) or {}).get("title"),
        creator_group_id=_creator_group_id(artifact),
        plan_item_id=str(artifact.plan_item_id),
        job_id=str(artifact.job_id) if artifact.job_id else None,
        variant_id=artifact.variant_id,
        format=edit_format,
        language=_language(artifact),
        media_mix=_media_mix(artifact.media_manifest),
        prompt_version=artifact.prompt_version,
        model_version=artifact.effective_model,
        created_at=artifact.created_at.isoformat(),
        duration_s=duration_s,
        render_generation=artifact.render_generation_id,
        render_receipt=receipt_projection,
        review_state=_review_state(current),  # type: ignore[arg-type]
        quality_signal=getattr(current.get("overall_quality"), "rating", None),
        edit_signal=getattr(current.get("instruction_fit"), "rating", None),
        reviewed_at=reviewed_at.isoformat() if reviewed_at else None,
        edit_count=edit_count,
        poster_url=poster_url,
        playback_url=playback_url,
        playback_identity=playback_identity,
        playback_expires_at=(
            (datetime.now(UTC) + timedelta(minutes=ADMIN_PLAYBACK_TTL_MIN)).isoformat()
            if playback_url
            else None
        ),
        timeline=timeline,
    )


@router.get("", response_model=ListArtifactsResponse, dependencies=[Depends(_require_admin)])
def list_edit_feedback(
    cursor: str | None = None,
    limit: int = Query(30, ge=1, le=100),
    format: str | None = None,
    language: str | None = None,
    media_mix: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    prompt_version: str | None = None,
    model_version: str | None = None,
    review_state: str | None = None,
    quality_signal: str | None = None,
    edit_signal: str | None = None,
    sampling: Literal["chronological", "stratified"] = "chronological",
) -> ListArtifactsResponse:
    from app.database import sync_session  # noqa: PLC0415
    from app.models import (  # noqa: PLC0415
        EditArtifact,
        EditFeedbackAnnotation,
        PlanItem,
        TrainingArtifactRetentionEvent,
    )
    from app.services.training_eligibility import eligible_artifact_ids  # noqa: PLC0415

    with sync_session() as db:
        query = (
            select(EditArtifact, PlanItem.edit_format)
            .join(PlanItem, PlanItem.id == EditArtifact.plan_item_id)
            .where(EditArtifact.artifact_kind == "final_render")
        )
        if format:
            query = query.where(PlanItem.edit_format == format)
        if date_from:
            query = query.where(EditArtifact.created_at >= date_from)
        if date_to:
            query = query.where(EditArtifact.created_at <= date_to)
        if prompt_version:
            query = query.where(EditArtifact.prompt_version == prompt_version)
        if model_version:
            query = query.where(EditArtifact.effective_model == model_version)
        if cursor and sampling == "chronological":
            cursor_at, cursor_id = _decode_cursor(cursor)
            query = query.where(
                or_(
                    EditArtifact.created_at < cursor_at,
                    and_(EditArtifact.created_at == cursor_at, EditArtifact.id < cursor_id),
                )
            )
        ordered_query = query.order_by(EditArtifact.created_at.desc(), EditArtifact.id.desc())
        if sampling == "stratified":
            raw_candidates = db.execute(ordered_query).all()
            candidates = raw_candidates
            has_more = False
        else:
            raw_candidates = db.execute(ordered_query.limit(limit + 1)).all()
            candidates = raw_candidates[:limit]
            has_more = len(raw_candidates) > limit
        candidate_ids = [artifact.id for artifact, _ in candidates]
        eligible_ids = eligible_artifact_ids(db, candidate_ids)
        from app.services.training_eligibility import retained_copy_is_eligible  # noqa: PLC0415

        eligible_ids = {
            artifact.id
            for artifact, _ in candidates
            if artifact.id in eligible_ids and retained_copy_is_eligible(db, artifact)
        }
        candidates = [
            (artifact, fmt) for artifact, fmt in candidates if artifact.id in eligible_ids
        ]

        annotation_rows = (
            db.execute(
                select(EditFeedbackAnnotation).where(
                    EditFeedbackAnnotation.artifact_id.in_(eligible_ids)
                )
            )
            .scalars()
            .all()
            if eligible_ids
            else []
        )
        retention_rows = (
            db.execute(
                select(TrainingArtifactRetentionEvent).where(
                    TrainingArtifactRetentionEvent.artifact_id.in_(eligible_ids)
                )
            )
            .scalars()
            .all()
            if eligible_ids
            else []
        )
        poster_artifacts = (
            db.execute(
                select(EditArtifact).where(
                    EditArtifact.parent_artifact_id.in_(eligible_ids),
                    EditArtifact.artifact_kind == "poster",
                )
            )
            .scalars()
            .all()
            if eligible_ids
            else []
        )
        poster_ids = {row.id for row in poster_artifacts}
        poster_retention = (
            db.execute(
                select(TrainingArtifactRetentionEvent).where(
                    TrainingArtifactRetentionEvent.artifact_id.in_(poster_ids)
                )
            )
            .scalars()
            .all()
            if poster_ids
            else []
        )
        annotations_by_artifact: dict[uuid.UUID, list[Any]] = defaultdict(list)
        retention_by_artifact: dict[uuid.UUID, list[Any]] = defaultdict(list)
        for row in annotation_rows:
            annotations_by_artifact[row.artifact_id].append(row)
        for row in retention_rows:
            retention_by_artifact[row.artifact_id].append(row)
        for row in poster_retention:
            retention_by_artifact[row.artifact_id].append(row)
        posters_by_parent = {row.parent_artifact_id: row for row in poster_artifacts}

        filtered: list[tuple[Any, str | None, ArtifactOut]] = []
        for artifact, fmt in candidates:
            payload = _artifact_out(
                artifact,
                edit_format=fmt,
                annotations=annotations_by_artifact[artifact.id],
                retention_rows=retention_by_artifact[artifact.id],
                include_receipt=False,
                poster_url=(
                    _playback(
                        posters_by_parent[artifact.id],
                        retention_by_artifact[posters_by_parent[artifact.id].id],
                    )[0]
                    if artifact.id in posters_by_parent
                    else None
                ),
            )
            if language and payload.language != language:
                continue
            if media_mix and payload.media_mix != media_mix:
                continue
            if review_state and review_state != "all" and payload.review_state != review_state:
                continue
            if (
                quality_signal
                and quality_signal != "all"
                and payload.quality_signal != quality_signal
            ):
                continue
            if edit_signal and edit_signal != "all" and payload.edit_signal != edit_signal:
                continue
            filtered.append((artifact, fmt, payload))
        next_cursor = None
        if sampling == "stratified":
            offset = _decode_stratified_cursor(cursor)
            ordered = _stratified_review_order(filtered)
            page = ordered[offset : offset + limit]
            if offset + limit < len(ordered):
                next_cursor = _stratified_cursor(offset + limit)
        else:
            page = filtered
            if has_more:
                # Advance on the raw page boundary. Revoked/ineligible rows and
                # Python-level filters must never strand older eligible renders.
                next_cursor = _next_chronological_cursor(raw_candidates, limit)
        return ListArtifactsResponse(
            items=[payload for _, _, payload in page],
            next_cursor=next_cursor,
            total=len(filtered) if sampling == "stratified" else len(page),
        )


def _load_eligible_artifact(db: Any, artifact_id: uuid.UUID) -> Any:
    from app.models import EditArtifact  # noqa: PLC0415
    from app.services.training_eligibility import retained_copy_is_eligible  # noqa: PLC0415

    artifact = db.get(EditArtifact, artifact_id)
    if artifact is None or not retained_copy_is_eligible(db, artifact):
        raise HTTPException(status_code=404, detail="edit artifact not found")
    return artifact


def _export_out(row: Any, *, include_download: bool) -> ExportResponse:
    download_url = None
    if (
        include_download
        and row.status == "ready"
        and row.storage_path
        and row.storage_generation
        and row.expires_at
        and row.expires_at > datetime.now(UTC)
    ):
        from app.storage import signed_get_url  # noqa: PLC0415

        download_url = signed_get_url(row.storage_path, expiration_minutes=15)
    return ExportResponse(
        id=str(row.id),
        status=row.status,
        export_format=row.export_format,
        row_count=row.row_count,
        content_hash=row.content_hash,
        failure_reason=row.failure_reason,
        expires_at=row.expires_at.isoformat() if row.expires_at else None,
        download_url=download_url,
    )


def _export_manifest_is_current(db: Any, row: Any) -> bool:
    from app.models import EditArtifact  # noqa: PLC0415
    from app.services.training_eligibility import retained_copy_is_eligible  # noqa: PLC0415

    raw_ids = (row.manifest or {}).get("artifact_ids", [])
    try:
        artifact_ids = {uuid.UUID(value) for value in raw_ids}
    except (TypeError, ValueError):
        return False
    artifacts = (
        db.execute(select(EditArtifact).where(EditArtifact.id.in_(artifact_ids))).scalars().all()
        if artifact_ids
        else []
    )
    return len(artifacts) == len(artifact_ids) and all(
        retained_copy_is_eligible(db, artifact) for artifact in artifacts
    )


@router.get(
    "/dataset-readiness",
    response_model=DatasetReadinessResponse,
    dependencies=[Depends(_require_admin)],
)
def get_dataset_readiness() -> DatasetReadinessResponse:
    from app.config import settings  # noqa: PLC0415
    from app.database import sync_session  # noqa: PLC0415
    from app.services.edit_training_dataset import dataset_readiness  # noqa: PLC0415
    from app.services.edit_training_exports import build_training_records  # noqa: PLC0415

    if len(settings.training_dataset_split_secret) < 16:
        raise HTTPException(
            status_code=503, detail="training dataset split secret is not configured"
        )
    with sync_session() as db:
        records, _ = build_training_records(
            db,
            secret=settings.training_dataset_split_secret,
        )
    return DatasetReadinessResponse.model_validate(
        dataset_readiness(records).model_dump(mode="json")
    )


@router.post(
    "/exports",
    response_model=ExportResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(_require_admin)],
)
def create_edit_training_export(req: CreateExportRequest) -> ExportResponse:
    from app.config import settings  # noqa: PLC0415
    from app.database import sync_session  # noqa: PLC0415
    from app.models import TrainingDatasetExport  # noqa: PLC0415
    from app.tasks.edit_training_exports import build_edit_training_export  # noqa: PLC0415

    if len(settings.training_dataset_split_secret) < 16:
        raise HTTPException(
            status_code=503, detail="training dataset split secret is not configured"
        )
    idempotency_key = req.idempotency_key.strip()
    if not idempotency_key:
        raise HTTPException(status_code=422, detail="idempotency key cannot be blank")
    with sync_session() as db:
        existing = db.execute(
            select(TrainingDatasetExport).where(
                TrainingDatasetExport.requested_by.is_(None),
                TrainingDatasetExport.idempotency_key == idempotency_key,
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.export_format != req.export_format:
                raise HTTPException(status_code=409, detail="idempotency key format mismatch")
            return _export_out(existing, include_download=False)
        row = TrainingDatasetExport(
            requested_by=None,
            purpose="edit_feedback_training",
            policy_version="edit-training-v1",
            dataset_schema_version="1",
            export_format=req.export_format,
            status="pending",
            creator_split_version="creator-hmac-v1",
            plan_item_split_version="creator-hmac-v1",
            manifest={},
            idempotency_key=idempotency_key,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        response = _export_out(row, include_download=False)
        export_id = str(row.id)
    build_edit_training_export.delay(export_id)
    return response


@router.post(
    "/internal-accounts/{creator_id}",
    response_model=InternalAccountResponse,
    dependencies=[Depends(_require_admin)],
)
def set_internal_account(
    creator_id: uuid.UUID,
    req: InternalAccountRequest,
) -> InternalAccountResponse:
    """Explicit operator grant; internal status is never inferred from email."""
    from app.config import settings  # noqa: PLC0415
    from app.database import sync_session  # noqa: PLC0415
    from app.models import InternalAccountGrant, User  # noqa: PLC0415

    if req.action == "grant" and len(settings.training_dataset_split_secret) < 16:
        raise HTTPException(
            status_code=503, detail="training dataset split secret is not configured"
        )
    with sync_session() as db:
        if db.get(User, creator_id) is None:
            raise HTTPException(status_code=404, detail="creator not found")
        existing = db.execute(
            select(InternalAccountGrant).where(
                InternalAccountGrant.creator_id == creator_id,
                InternalAccountGrant.idempotency_key == req.idempotency_key.strip(),
            )
        ).scalar_one_or_none()
        if existing is not None:
            expected_active = req.action == "grant"
            if (existing.status == "active") != expected_active:
                raise HTTPException(status_code=409, detail="grant idempotency key mismatch")
            return InternalAccountResponse(
                creator_id=str(creator_id),
                active=existing.status == "active",
                grant_id=str(existing.id),
            )
        active = db.execute(
            select(InternalAccountGrant)
            .where(
                InternalAccountGrant.creator_id == creator_id,
                InternalAccountGrant.status == "active",
            )
            .with_for_update()
        ).scalar_one_or_none()
        if req.action == "grant":
            if active is not None:
                return InternalAccountResponse(
                    creator_id=str(creator_id), active=True, grant_id=str(active.id)
                )
            active = InternalAccountGrant(
                creator_id=creator_id,
                status="active",
                granted_by="admin_api",
                reason=(req.reason or "").strip() or None,
                idempotency_key=req.idempotency_key.strip(),
            )
            db.add(active)
            db.commit()
            db.refresh(active)
            from app.tasks.edit_training_artifacts import (  # noqa: PLC0415
                backfill_edit_training_artifacts,
            )

            backfill_edit_training_artifacts.delay(str(creator_id), 300)
            return InternalAccountResponse(
                creator_id=str(creator_id), active=True, grant_id=str(active.id)
            )
        if active is None:
            return InternalAccountResponse(creator_id=str(creator_id), active=False)
        active.status = "revoked"
        active.revoked_at = datetime.now(UTC)
        db.commit()
        grant_id = str(active.id)

    from app.tasks.edit_training_artifacts import purge_edit_training_artifacts  # noqa: PLC0415

    purge_edit_training_artifacts.delay(str(creator_id), None, grant_id)
    return InternalAccountResponse(creator_id=str(creator_id), active=False, grant_id=grant_id)


@router.get(
    "/exports/{export_id}",
    response_model=ExportResponse,
    dependencies=[Depends(_require_admin)],
)
def get_edit_training_export(export_id: uuid.UUID) -> ExportResponse:
    from app.database import sync_session  # noqa: PLC0415
    from app.models import TrainingDatasetExport  # noqa: PLC0415

    with sync_session() as db:
        row = db.get(TrainingDatasetExport, export_id, with_for_update=True)
        if row is None:
            raise HTTPException(status_code=404, detail="training export not found")
        if row.status == "ready" and not _export_manifest_is_current(db, row):
            row.status = "revoked"
            row.failure_reason = "artifact_eligibility_revoked"
            db.commit()
            if row.storage_path and row.storage_generation:
                from app.storage import delete_object_generation_best_effort  # noqa: PLC0415

                delete_object_generation_best_effort(
                    row.storage_path,
                    generation=row.storage_generation,
                )
        return _export_out(row, include_download=True)


@router.get(
    "/{artifact_id}",
    response_model=ArtifactDetailResponse,
    dependencies=[Depends(_require_admin)],
)
def get_edit_feedback(artifact_id: uuid.UUID) -> ArtifactDetailResponse:
    from app.database import sync_session  # noqa: PLC0415
    from app.models import (  # noqa: PLC0415
        EditArtifact,
        EditFeedbackAnnotation,
        EditInteractionReceipt,
        PlanItem,
        TrainingArtifactRetentionEvent,
    )

    with sync_session() as db:
        artifact = _load_eligible_artifact(db, artifact_id)
        edit_format = db.execute(
            select(PlanItem.edit_format).where(PlanItem.id == artifact.plan_item_id)
        ).scalar_one_or_none()
        annotations = (
            db.execute(
                select(EditFeedbackAnnotation)
                .where(EditFeedbackAnnotation.artifact_id == artifact.id)
                .order_by(EditFeedbackAnnotation.created_at.asc(), EditFeedbackAnnotation.id.asc())
            )
            .scalars()
            .all()
        )
        retention_rows = (
            db.execute(
                select(TrainingArtifactRetentionEvent).where(
                    TrainingArtifactRetentionEvent.artifact_id == artifact.id
                )
            )
            .scalars()
            .all()
        )
        poster = db.execute(
            select(EditArtifact).where(
                EditArtifact.parent_artifact_id == artifact.id,
                EditArtifact.artifact_kind == "poster",
            )
        ).scalar_one_or_none()
        poster_retention = (
            db.execute(
                select(TrainingArtifactRetentionEvent).where(
                    TrainingArtifactRetentionEvent.artifact_id == poster.id
                )
            )
            .scalars()
            .all()
            if poster is not None
            else []
        )
        current, superseded_by = _current_annotations(annotations)
        payload = _artifact_out(
            artifact,
            edit_format=edit_format,
            annotations=annotations,
            retention_rows=retention_rows,
            include_receipt=True,
            poster_url=_playback(poster, poster_retention)[0] if poster is not None else None,
        )
        execution = None
        if artifact.job_id and artifact.variant_id:
            grant_predicate = (
                EditInteractionReceipt.consent_event_id == artifact.consent_event_id
                if artifact.eligibility_basis == "training_consent"
                else EditInteractionReceipt.internal_grant_id == artifact.internal_grant_id
            )
            row = db.execute(
                select(EditInteractionReceipt)
                .where(
                    EditInteractionReceipt.job_id == artifact.job_id,
                    EditInteractionReceipt.variant_id == artifact.variant_id,
                    EditInteractionReceipt.event_kind == "save_link",
                    EditInteractionReceipt.eligibility_basis == artifact.eligibility_basis,
                    grant_predicate,
                    EditInteractionReceipt.after_revision_hash
                    == artifact.render_receipt.get("revision_hash"),
                )
                .order_by(EditInteractionReceipt.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            if row is not None:
                execution = {
                    "utterance": row.utterance,
                    "inferred_intent": row.inferred_intent,
                    "model_reply": row.model_reply,
                    "proposal_outcome": row.proposal_outcome,
                    "execution_outcome": row.execution_outcome,
                    "rejection_reasons": row.rejection_reasons,
                    "before_revision_hash": row.before_revision_hash,
                    "after_revision_hash": row.after_revision_hash,
                }
        return ArtifactDetailResponse(
            artifact=payload,
            annotations=[_annotation_out(row, superseded_by) for row in annotations],
            timeline=payload.timeline,
            proposal=getattr(artifact, "direction_snapshot", None),
            execution_receipt=execution,
        )


def _new_annotation_row(db: Any, artifact: Any, req: SaveAnnotationRequest) -> Any:
    """Build one append-only annotation against the locked artifact state."""
    from app.models import EditFeedbackAnnotation  # noqa: PLC0415

    duration_ms = int(artifact.duration_ms or 0)
    start_ms = round(req.frame_start_s * 1000) if req.frame_start_s is not None else None
    end_ms = round(req.frame_end_s * 1000) if req.frame_end_s is not None else None
    if end_ms is not None and end_ms > duration_ms:
        raise HTTPException(status_code=422, detail="annotation frame range exceeds artifact")

    rows = (
        db.execute(
            select(EditFeedbackAnnotation)
            .where(
                EditFeedbackAnnotation.artifact_id == artifact.id,
                EditFeedbackAnnotation.dimension == req.dimension,
            )
            .with_for_update()
        )
        .scalars()
        .all()
    )
    current, _ = _current_annotations(rows)
    current_row = current.get(req.dimension)
    supersedes = None
    if req.supersedes_annotation_id:
        try:
            supersedes_id = uuid.UUID(req.supersedes_annotation_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid superseded annotation") from exc
        if current_row is None or current_row.id != supersedes_id:
            raise HTTPException(status_code=409, detail="superseded annotation is stale")
        supersedes = current_row
    elif current_row is not None:
        raise HTTPException(status_code=409, detail="annotation state changed")

    return EditFeedbackAnnotation(
        creator_id=artifact.creator_id,
        plan_item_id=artifact.plan_item_id,
        artifact_id=artifact.id,
        dimension=req.dimension,
        rating=req.rating,
        rationale=(req.rationale or "").strip() or None,
        frame_start_ms=start_ms,
        frame_end_ms=end_ms,
        reviewer_identity="emir",
        supersedes_annotation_id=supersedes.id if supersedes else None,
    )


@router.post(
    "/{artifact_id}/annotations",
    response_model=SaveAnnotationResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_require_admin)],
)
def save_edit_feedback_annotation(
    artifact_id: uuid.UUID,
    req: SaveAnnotationRequest,
) -> SaveAnnotationResponse:
    from app.database import sync_session  # noqa: PLC0415
    from app.models import EditArtifact  # noqa: PLC0415

    with sync_session() as db:
        artifact = _load_eligible_artifact(db, artifact_id)
        db.execute(
            select(EditArtifact.id).where(EditArtifact.id == artifact.id).with_for_update()
        ).scalar_one()
        row = _new_annotation_row(db, artifact, req)
        db.add(row)
        db.commit()
        db.refresh(row)
        return SaveAnnotationResponse(annotation=_annotation_out(row, {}))


@router.post(
    "/{artifact_id}/annotations/bulk",
    response_model=SaveAnnotationsBulkResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_require_admin)],
)
def save_edit_feedback_annotations_bulk(
    artifact_id: uuid.UUID,
    req: SaveAnnotationsBulkRequest,
) -> SaveAnnotationsBulkResponse:
    """Append an explicit set of distinct ratings in one transaction."""
    from app.database import sync_session  # noqa: PLC0415
    from app.models import EditArtifact  # noqa: PLC0415

    with sync_session() as db:
        artifact = _load_eligible_artifact(db, artifact_id)
        db.execute(
            select(EditArtifact.id).where(EditArtifact.id == artifact.id).with_for_update()
        ).scalar_one()
        rows = [_new_annotation_row(db, artifact, annotation) for annotation in req.annotations]
        db.add_all(rows)
        db.commit()
        for row in rows:
            db.refresh(row)
        return SaveAnnotationsBulkResponse(annotations=[_annotation_out(row, {}) for row in rows])
