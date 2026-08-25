"""Approval-gated off-plan creator workspace intake."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentUser
from app.config import settings
from app.database import get_db
from app.models import ContentPlan, CreatorWorkspaceProposal, Job, PlanItem
from app.services.plan_clips import ClipAssignment, ClipAssignmentError, set_item_clips
from app.tasks.creator_workspace import detect_plan_relevance

router = APIRouter()

_MAX_MEDIA = 50


def _opaque(value: str, field_name: str) -> str:
    value = value.strip()
    if not value or "://" in value or value.startswith(("/", "gs:", "s3:")):
        raise ValueError(f"{field_name} must be an opaque media identity")
    return value


class WorkspaceCreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    media_ids: list[str] = Field(min_length=1, max_length=_MAX_MEDIA)
    idempotency_key: str = Field(min_length=1, max_length=160)

    @field_validator("media_ids")
    @classmethod
    def _media_ids_are_opaque(cls, values: list[str]) -> list[str]:
        values = [_opaque(value, "media_ids") for value in values]
        if len(values) != len(set(values)):
            raise ValueError("media_ids must not contain duplicates")
        return values

    @field_validator("idempotency_key")
    @classmethod
    def _idempotency_is_opaque(cls, value: str) -> str:
        return _opaque(value, "idempotency_key")


class WorkspaceDecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_proposal_hash: str = Field(min_length=64, max_length=64)
    decision: Literal["accept_existing", "accept_new_topic", "reject"]
    client_event_id: str = Field(min_length=1, max_length=160)

    @field_validator("expected_proposal_hash")
    @classmethod
    def _hash_is_lower_sha256(cls, value: str) -> str:
        if any(char not in "0123456789abcdef" for char in value):
            raise ValueError("expected_proposal_hash must be lowercase SHA-256")
        return value

    @field_validator("client_event_id")
    @classmethod
    def _event_is_opaque(cls, value: str) -> str:
        return _opaque(value, "client_event_id")


class WorkspaceProposalResponse(BaseModel):
    id: str
    proposal_id: str
    creator_id: str
    plan_id: str
    ownership_epoch: int
    idempotency_key: str
    request_digest: str
    media_ids: list[str]
    status: str
    relevance: str | None = None
    target_plan_item_id: str | None = None
    topic: str | None = None
    rationale: str | None = None
    confidence: float | None = None
    proposal_hash: str | None = None
    error_code: str | None = None
    decision: str | None = None
    result_plan_item_id: str | None = None


def _response(row: CreatorWorkspaceProposal) -> WorkspaceProposalResponse:
    return WorkspaceProposalResponse(
        id=str(row.id),
        proposal_id=str(row.id),
        creator_id=str(row.creator_id),
        plan_id=str(row.plan_id),
        ownership_epoch=int(row.ownership_epoch),
        idempotency_key=row.idempotency_key,
        request_digest=row.request_digest,
        media_ids=list(row.media_ids or []),
        status=row.status,
        relevance=row.relevance,
        target_plan_item_id=str(row.target_plan_item_id) if row.target_plan_item_id else None,
        topic=row.topic,
        rationale=row.rationale,
        confidence=row.confidence,
        proposal_hash=row.proposal_hash,
        error_code=row.error_code,
        decision=row.decision,
        result_plan_item_id=str(row.result_plan_item_id) if row.result_plan_item_id else None,
    )


def _request_digest(plan_id: uuid.UUID, epoch: int, media_ids: list[str]) -> str:
    payload = {"plan_id": str(plan_id), "ownership_epoch": epoch, "media_ids": media_ids}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _require_workspace_enabled() -> None:
    if not settings.main_creator_agent_freeform_uploads_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Creator workspace uploads are not available",
        )


async def _owned_plan(
    plan_id: str,
    user_id: uuid.UUID,
    db: AsyncSession,
    *,
    for_update: bool = False,
) -> ContentPlan:
    try:
        pid = uuid.UUID(plan_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="bad plan id") from exc
    stmt = select(ContentPlan).where(ContentPlan.id == pid, ContentPlan.user_id == user_id)
    if for_update:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    plan = (await db.execute(stmt)).scalar_one_or_none()
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found")
    if plan.ownership_quarantined_at is not None:
        raise HTTPException(status_code=409, detail="Plan ownership is temporarily quarantined")
    return plan


@router.post(
    "/{plan_id}/workspace/relevance-proposals",
    response_model=WorkspaceProposalResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_relevance_proposal(
    plan_id: str,
    body: WorkspaceCreateBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> WorkspaceProposalResponse:
    """Create/reuse a pending proposal and enqueue analysis.

    Upload IDs are Job IDs from the authenticated upload flow.  They are
    resolved once, owner-checked, and snapshotted; no plan or render mutation
    occurs in this endpoint.
    """

    _require_workspace_enabled()
    plan = await _owned_plan(plan_id, user.id, db, for_update=True)
    epoch = int(plan.ownership_epoch or 0)
    digest = _request_digest(plan.id, epoch, body.media_ids)
    existing = (
        await db.execute(
            select(CreatorWorkspaceProposal).where(
                CreatorWorkspaceProposal.creator_id == user.id,
                CreatorWorkspaceProposal.idempotency_key == body.idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.request_digest != digest or existing.plan_id != plan.id:
            raise HTTPException(
                status_code=409,
                detail="Idempotency key reused with different media",
            )
        return _response(existing)

    ids: list[uuid.UUID] = []
    for media_id in body.media_ids:
        try:
            ids.append(uuid.UUID(media_id))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="media_ids must reference uploads") from exc
    jobs = (
        (await db.execute(select(Job).where(Job.id.in_(ids), Job.user_id == user.id)))
        .scalars()
        .all()
    )
    by_id = {str(job.id): job for job in jobs}
    if len(by_id) != len(ids):
        raise HTTPException(status_code=404, detail="One or more uploads were not found")
    if any(
        not job.raw_storage_path or not str(job.raw_storage_path).startswith(f"{user.id}/")
        for job in jobs
    ):
        raise HTTPException(status_code=409, detail="One or more uploads are not attachable")
    snapshot = [
        {
            "media_id": media_id,
            "source_job_id": media_id,
            "gcs_path": by_id[media_id].raw_storage_path,
            "gcs_generation": None,
            "kind": "video",
            "source_filename": None,
        }
        for media_id in body.media_ids
    ]
    row = CreatorWorkspaceProposal(
        creator_id=user.id,
        plan_id=plan.id,
        ownership_epoch=epoch,
        idempotency_key=body.idempotency_key,
        request_digest=digest,
        media_ids=list(body.media_ids),
        media_snapshot=snapshot,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    detect_plan_relevance.delay(str(row.id))
    return _response(row)


@router.get(
    "/{plan_id}/workspace/relevance-proposals/{proposal_id}",
    response_model=WorkspaceProposalResponse,
)
async def get_relevance_proposal(
    plan_id: str,
    proposal_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> WorkspaceProposalResponse:
    _require_workspace_enabled()
    plan = await _owned_plan(plan_id, user.id, db)
    try:
        proposal_uuid = uuid.UUID(proposal_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="bad proposal id") from exc
    row = (
        await db.execute(
            select(CreatorWorkspaceProposal).where(
                CreatorWorkspaceProposal.id == proposal_uuid,
                CreatorWorkspaceProposal.plan_id == plan.id,
                CreatorWorkspaceProposal.creator_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Proposal not found")
    return _response(row)


@router.post(
    "/{plan_id}/workspace/relevance-proposals/{proposal_id}/decision",
    response_model=WorkspaceProposalResponse,
)
async def decide_relevance_proposal(
    plan_id: str,
    proposal_id: str,
    body: WorkspaceDecisionBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> WorkspaceProposalResponse:
    """Apply exactly one explicit decision; all mutations are in one transaction."""

    _require_workspace_enabled()
    plan = await _owned_plan(plan_id, user.id, db, for_update=True)
    try:
        proposal_uuid = uuid.UUID(proposal_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="bad proposal id") from exc
    row = (
        await db.execute(
            select(CreatorWorkspaceProposal)
            .where(
                CreatorWorkspaceProposal.id == proposal_uuid,
                CreatorWorkspaceProposal.plan_id == plan.id,
                CreatorWorkspaceProposal.creator_id == user.id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Proposal not found")
    if row.status in {"approved", "rejected"}:
        if row.decision == body.decision and row.decision_client_event_id == body.client_event_id:
            return _response(row)
        raise HTTPException(status_code=409, detail="Proposal already decided")
    if row.status != "ready":
        raise HTTPException(status_code=409, detail="Proposal is not ready for approval")
    if int(row.ownership_epoch) != int(plan.ownership_epoch or 0):
        raise HTTPException(status_code=409, detail="Proposal ownership epoch is stale")
    if row.proposal_hash != body.expected_proposal_hash:
        raise HTTPException(status_code=409, detail="Proposal changed; refresh before deciding")
    if row.relevance == "existing_item" and not row.target_plan_item_id:
        raise HTTPException(status_code=409, detail="Proposal target is incomplete")
    if body.decision == "accept_existing" and row.relevance != "existing_item":
        raise HTTPException(status_code=409, detail="Proposal does not target an existing item")
    if body.decision == "accept_new_topic" and row.relevance != "new_topic":
        raise HTTPException(status_code=409, detail="Proposal does not target a new topic")

    # Re-fence every upload identity at the approval boundary.  A path copied
    # into the proposal is not sufficient if an upload was deleted, retargeted,
    # or reassigned while the creator was reviewing the proposal.
    snapshots = list(row.media_snapshot or [])
    source_ids: list[uuid.UUID] = []
    try:
        source_ids = [uuid.UUID(str(media["source_job_id"])) for media in snapshots]
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=409, detail="Proposal media identity is invalid") from exc
    live_jobs = (
        (await db.execute(select(Job).where(Job.id.in_(source_ids), Job.user_id == user.id)))
        .scalars()
        .all()
    )
    live_by_id = {str(job.id): job for job in live_jobs}
    for media in snapshots:
        job = live_by_id.get(str(media["source_job_id"]))
        if job is None or job.raw_storage_path != media.get("gcs_path"):
            raise HTTPException(status_code=409, detail="Proposal media is no longer owned")

    result_item_id: uuid.UUID | None = None
    if body.decision == "accept_existing":
        item = (
            await db.execute(
                select(PlanItem)
                .where(
                    PlanItem.id == row.target_plan_item_id,
                    PlanItem.content_plan_id == plan.id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if item is None:
            raise HTTPException(status_code=409, detail="Target plan item no longer exists")
        result_item_id = item.id
    elif body.decision == "accept_new_topic":
        max_position = (
            await db.execute(
                select(func.max(PlanItem.position)).where(PlanItem.content_plan_id == plan.id)
            )
        ).scalar_one()
        item = PlanItem(
            content_plan_id=plan.id,
            idea=(row.topic or "New footage")[:500],
            position=int(max_position or 0) + 1,
            day_index=None,
            edit_format="montage",
            item_status="awaiting_clips",
            user_edited=True,
        )
        db.add(item)
        await db.flush()
        result_item_id = item.id
    if body.decision != "reject":
        try:
            existing_assignments = [
                ClipAssignment(
                    gcs_path=str(assignment["gcs_path"]),
                    shot_id=assignment.get("shot_id"),
                    user_note=str(assignment.get("user_note") or ""),
                    machine_matched=bool(assignment.get("machine_matched", False)),
                    media_id=(str(assignment["media_id"]) if assignment.get("media_id") else None),
                )
                for assignment in (item.clip_assignments or [])
                if isinstance(assignment, dict) and assignment.get("gcs_path")
            ]
            set_item_clips(
                item,
                existing_assignments
                + [
                    ClipAssignment(
                        gcs_path=str(media["gcs_path"]),
                        media_id=str(media["media_id"]),
                    )
                    for media in snapshots
                ],
            )
        except (KeyError, ClipAssignmentError) as exc:
            raise HTTPException(
                status_code=409,
                detail="Proposal media is no longer attachable",
            ) from exc
        # Attaching footage invalidates any prior
        # conformance evidence; this route intentionally does not enqueue a
        # render or analysis job as part of approval.
        item.conformance = None
    row.status = "rejected" if body.decision == "reject" else "approved"
    row.decision = body.decision
    row.decision_client_event_id = body.client_event_id
    row.result_plan_item_id = result_item_id
    await db.commit()
    return _response(row)


__all__ = [
    "WorkspaceCreateBody",
    "WorkspaceDecisionBody",
    "WorkspaceProposalResponse",
    "router",
]
