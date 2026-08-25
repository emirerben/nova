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

from app.agents._schemas.user_style import UserStyle
from app.agents.music_matcher import _sanitize_text
from app.auth import CurrentUser
from app.config import settings
from app.database import get_db
from app.models import (
    ContentPlan,
    CreatorAgentSession,
    CreatorWorkspaceDeliverable,
    CreatorWorkspacePreferenceSignal,
    CreatorWorkspaceProposal,
    CreatorWorkspaceReceipt,
    Job,
    Persona,
    PlanItem,
    VideoFeedback,
)
from app.routes.personas import StyleEdit
from app.services.feedback_summary import MAX_NOTES_IN_SUMMARY, build_preference_summary
from app.services.job_status import PLAN_ITEM_JOB_FAILED, PLAN_ITEM_JOB_READY
from app.services.plan_clips import ClipAssignment, ClipAssignmentError, set_item_clips
from app.tasks.creator_workspace import detect_plan_relevance

router = APIRouter()

_MAX_MEDIA = 50
_MAX_DELIVERABLES = 50


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


class WorkspaceReceiptCreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_item_ids: list[str] = Field(min_length=1, max_length=_MAX_DELIVERABLES)
    idempotency_key: str = Field(min_length=1, max_length=160)

    @field_validator("plan_item_ids")
    @classmethod
    def _item_ids_are_opaque(cls, values: list[str]) -> list[str]:
        values = [_opaque(value, "plan_item_ids") for value in values]
        if len(values) != len(set(values)):
            raise ValueError("plan_item_ids must not contain duplicates")
        return values

    @field_validator("idempotency_key")
    @classmethod
    def _receipt_idempotency_is_opaque(cls, value: str) -> str:
        return _opaque(value, "idempotency_key")


class WorkspacePreferenceSignalBody(BaseModel):
    """One explicit creator note, optionally paired with a style edit."""

    model_config = ConfigDict(extra="forbid")

    note: str = Field(min_length=1, max_length=1200)
    client_event_id: str = Field(min_length=1, max_length=160)
    receipt_id: str | None = None
    style_edit: StyleEdit | None = None

    @field_validator("client_event_id")
    @classmethod
    def _preference_event_is_opaque(cls, value: str) -> str:
        return _opaque(value, "client_event_id")

    @field_validator("receipt_id")
    @classmethod
    def _preference_receipt_is_opaque(cls, value: str | None) -> str | None:
        return _opaque(value, "receipt_id") if value is not None else None

    @field_validator("note")
    @classmethod
    def _note_is_creator_text(cls, value: str) -> str:
        clean = _sanitize_text(value)
        if not clean:
            raise ValueError("note must contain creator-authored text")
        return clean[:1200]


class WorkspaceDeliverableResponse(BaseModel):
    deliverable_id: str
    plan_item_id: str
    creator_session_id: str
    ownership_epoch: int
    session_revision: int
    status: str
    job_id: str | None = None
    variant_id: str | None = None
    render_generation_id: str | None = None
    generation_receipt: dict | None = None


class WorkspaceReceiptResponse(BaseModel):
    receipt_id: str
    creator_id: str
    plan_id: str
    ownership_epoch: int
    idempotency_key: str
    request_digest: str
    status: str
    deliverables: list[WorkspaceDeliverableResponse]
    preference_summary: str | None = None
    style: dict | None = None


class WorkspacePreferenceSignalResponse(BaseModel):
    signal_id: str
    creator_id: str
    plan_id: str
    ownership_epoch: int
    source: Literal["creator_explicit"]
    note: str
    style: dict | None = None
    preference_summary: str | None = None


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


def _media_paths_already_attached(items: list[PlanItem], paths: set[str]) -> bool:
    """Return whether any source path is already owned by another item."""

    for item in items:
        attached = {str(path) for path in (item.clip_gcs_paths or []) if path}
        attached.update(
            str(assignment["gcs_path"])
            for assignment in (item.clip_assignments or [])
            if isinstance(assignment, dict) and assignment.get("gcs_path")
        )
        if paths & attached:
            return True
    return False


async def _enqueue_relevance_or_mark_failed(
    db: AsyncSession, row: CreatorWorkspaceProposal
) -> None:
    """Publish one deterministic analysis task, making broker failure visible.

    The proposal row is durable before this function is called. A retry of the
    same idempotency key can therefore safely publish the task again; the
    worker's processing claim prevents duplicate classifier work.
    """

    try:
        detect_plan_relevance.apply_async(
            args=[str(row.id)],
            task_id=f"creator-relevance-{row.id}",
        )
    except Exception:  # noqa: BLE001 - expose queue failure through the proposal
        row.status = "failed"
        row.error_code = "relevance_dispatch_failed"
        await db.commit()


def _require_workspace_enabled() -> None:
    if not settings.main_creator_agent_freeform_uploads_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Creator workspace uploads are not available",
        )


def _require_coordination_enabled() -> None:
    if not settings.main_creator_agent_workspace_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Creator workspace coordination is not available",
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
        if existing.status == "failed" and existing.error_code == "relevance_dispatch_failed":
            existing.status = "pending"
            existing.error_code = None
            await db.commit()
            await _enqueue_relevance_or_mark_failed(db, existing)
        elif existing.status == "pending":
            # This covers a process crash after the proposal commit and before
            # broker publication. Processing claims make this retry safe.
            await _enqueue_relevance_or_mark_failed(db, existing)
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
    await _enqueue_relevance_or_mark_failed(db, row)
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
    # Source Jobs are the shared serialization point across plans. Locking them
    # in deterministic UUID order prevents concurrent approvals from both
    # passing the cross-item path scan before either attachment commits.
    source_ids = sorted(set(source_ids), key=str)
    live_jobs = (
        (
            await db.execute(
                select(Job)
                .where(Job.id.in_(source_ids), Job.user_id == user.id)
                .order_by(Job.id)
                .with_for_update()
            )
        )
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
        other_items = (
            (
                await db.execute(
                    select(PlanItem)
                    .join(ContentPlan, ContentPlan.id == PlanItem.content_plan_id)
                    .where(
                        ContentPlan.user_id == user.id,
                        PlanItem.id != item.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        if _media_paths_already_attached(
            other_items, {str(media["gcs_path"]) for media in snapshots}
        ):
            raise HTTPException(
                status_code=409,
                detail="Proposal media is already attached to another plan item",
            )
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


def _workspace_request_digest(
    plan_id: uuid.UUID, ownership_epoch: int, plan_item_ids: list[str]
) -> str:
    encoded = json.dumps(
        {
            "plan_id": str(plan_id),
            "ownership_epoch": ownership_epoch,
            "plan_item_ids": plan_item_ids,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _preference_request_digest(
    plan_id: uuid.UUID, ownership_epoch: int, note: str, style_edit: StyleEdit | None
) -> str:
    encoded = json.dumps(
        {
            "plan_id": str(plan_id),
            "ownership_epoch": ownership_epoch,
            "note": note,
            "style_edit": style_edit.model_dump(mode="json", exclude_none=True)
            if style_edit
            else None,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _job_state(job: Job | None, session: CreatorAgentSession) -> str:
    if job is not None and job.status in PLAN_ITEM_JOB_FAILED | {"failed"}:
        return "failed"
    if job is not None and job.status in PLAN_ITEM_JOB_READY | {"ready"}:
        if not session.target_generation_id or not session.target_variant_id:
            return "stale"
        variants = (job.assembly_plan or {}).get("variants") or []
        exact = next(
            (
                variant
                for variant in variants
                if isinstance(variant, dict)
                and variant.get("variant_id") == session.target_variant_id
            ),
            None,
        )
        if exact is None or exact.get("render_generation_id") != session.target_generation_id:
            return "stale"
        if exact.get("render_status") == "failed":
            return "failed"
        return "ready" if exact.get("render_status") == "ready" else "processing"
    if job is not None or session.status in {
        "executing",
        "rendering",
        "reviewing",
        "revising",
    }:
        return "processing"
    return "pending"


async def _workspace_response(
    db: AsyncSession, receipt: CreatorWorkspaceReceipt, plan: ContentPlan
) -> WorkspaceReceiptResponse:
    deliverables = sorted(receipt.deliverables, key=lambda row: row.position)
    session_ids = [row.creator_session_id for row in deliverables]
    sessions = (
        (
            await db.execute(
                select(CreatorAgentSession).where(CreatorAgentSession.id.in_(session_ids))
            )
        )
        .scalars()
        .all()
        if session_ids
        else []
    )
    sessions_by_id = {row.id: row for row in sessions}
    job_ids = [session.target_job_id for session in sessions if session.target_job_id]
    jobs = (
        (await db.execute(select(Job).where(Job.id.in_(job_ids)))).scalars().all()
        if job_ids
        else []
    )
    jobs_by_id = {row.id: row for row in jobs}
    receipt_stale = int(plan.ownership_epoch or 0) != int(receipt.ownership_epoch)
    any_stale = receipt_stale
    output: list[WorkspaceDeliverableResponse] = []
    states: list[str] = []
    for row in deliverables:
        session = sessions_by_id.get(row.creator_session_id)
        if (
            session is None
            or session.creator_id != receipt.creator_id
            or session.plan_item_id != row.plan_item_id
            or int(session.ownership_epoch) != int(row.ownership_epoch)
        ):
            any_stale = True
            states.append("stale")
            output.append(
                WorkspaceDeliverableResponse(
                    deliverable_id=str(row.id),
                    plan_item_id=str(row.plan_item_id),
                    creator_session_id=str(row.creator_session_id),
                    ownership_epoch=int(row.ownership_epoch),
                    session_revision=int(row.session_revision),
                    status="stale",
                    job_id=str(row.job_id) if row.job_id else None,
                    variant_id=row.variant_id,
                    render_generation_id=row.render_generation_id,
                    generation_receipt=row.generation_receipt,
                )
            )
            continue
        job = jobs_by_id.get(session.target_job_id) if session.target_job_id else None
        if job is not None and (
            job.user_id != receipt.creator_id
            or job.content_plan_item_id != row.plan_item_id
            or int(job.content_plan_ownership_epoch or -1) != int(row.ownership_epoch)
        ):
            deliverable_stale = True
            state = "stale"
        else:
            deliverable_stale = False
            state = _job_state(job, session)
            if state == "stale":
                deliverable_stale = True
        if receipt_stale or deliverable_stale:
            state = "stale"
            any_stale = True
        states.append(state)
        job_id = session.target_job_id or row.job_id
        variant_id = session.target_variant_id or row.variant_id
        generation_id = session.target_generation_id or row.render_generation_id
        generation_receipt = row.generation_receipt
        if generation_id and job_id and variant_id:
            generation_receipt = {
                "job_id": str(job_id),
                "variant_id": variant_id,
                "render_generation_id": generation_id,
                "ownership_epoch": int(row.ownership_epoch),
                "session_revision": int(session.revision),
            }
        output.append(
            WorkspaceDeliverableResponse(
                deliverable_id=str(row.id),
                plan_item_id=str(row.plan_item_id),
                creator_session_id=str(row.creator_session_id),
                ownership_epoch=int(row.ownership_epoch),
                session_revision=int(session.revision),
                status=state,
                job_id=str(job_id) if job_id else None,
                variant_id=variant_id,
                render_generation_id=generation_id,
                generation_receipt=generation_receipt,
            )
        )
    if any_stale:
        receipt_status = "stale"
    elif any(state == "failed" for state in states):
        receipt_status = "failed"
    elif states and all(state == "ready" for state in states):
        receipt_status = "ready"
    elif any(state == "processing" for state in states):
        receipt_status = "processing"
    else:
        receipt_status = "pending"
    persona = (
        await db.execute(select(Persona).where(Persona.user_id == receipt.creator_id))
    ).scalar_one_or_none()
    return WorkspaceReceiptResponse(
        receipt_id=str(receipt.id),
        creator_id=str(receipt.creator_id),
        plan_id=str(receipt.plan_id),
        ownership_epoch=int(receipt.ownership_epoch),
        idempotency_key=receipt.idempotency_key,
        request_digest=receipt.request_digest,
        status=receipt_status,
        deliverables=output,
        preference_summary=plan.preference_summary,
        style=dict(persona.style) if persona and persona.style else None,
    )


async def _latest_workspace_receipt(
    plan: ContentPlan, user: CurrentUser, db: AsyncSession, receipt_id: str | None = None
) -> WorkspaceReceiptResponse:
    stmt = select(CreatorWorkspaceReceipt).where(
        CreatorWorkspaceReceipt.plan_id == plan.id,
        CreatorWorkspaceReceipt.creator_id == user.id,
    )
    if receipt_id is not None:
        try:
            parsed_id = uuid.UUID(receipt_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="bad receipt id") from exc
        stmt = stmt.where(CreatorWorkspaceReceipt.id == parsed_id)
    else:
        stmt = stmt.order_by(CreatorWorkspaceReceipt.created_at.desc()).limit(1)
    receipt = (await db.execute(stmt)).scalar_one_or_none()
    if receipt is None:
        raise HTTPException(status_code=404, detail="Workspace receipt not found")
    return await _workspace_response(db, receipt, plan)


@router.post(
    "/{plan_id}/workspace/receipts",
    response_model=WorkspaceReceiptResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_workspace_receipt(
    plan_id: str,
    body: WorkspaceReceiptCreateBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> WorkspaceReceiptResponse:
    """Create/reuse a read-only coordination receipt for child PlanItems."""

    _require_coordination_enabled()
    plan = await _owned_plan(plan_id, user.id, db, for_update=True)
    digest = _workspace_request_digest(plan.id, int(plan.ownership_epoch or 0), body.plan_item_ids)
    existing = (
        await db.execute(
            select(CreatorWorkspaceReceipt).where(
                CreatorWorkspaceReceipt.creator_id == user.id,
                CreatorWorkspaceReceipt.plan_id == plan.id,
                CreatorWorkspaceReceipt.idempotency_key == body.idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.request_digest != digest:
            raise HTTPException(
                status_code=409, detail="Idempotency key reused with different items"
            )
        return await _workspace_response(db, existing, plan)

    try:
        item_ids = [uuid.UUID(item_id) for item_id in body.plan_item_ids]
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail="plan_item_ids must reference plan items"
        ) from exc
    items = (
        (
            await db.execute(
                select(PlanItem).where(
                    PlanItem.id.in_(item_ids), PlanItem.content_plan_id == plan.id
                )
            )
        )
        .scalars()
        .all()
    )
    if len(items) != len(item_ids):
        raise HTTPException(status_code=404, detail="One or more plan items were not found")
    sessions = (
        (
            await db.execute(
                select(CreatorAgentSession)
                .where(
                    CreatorAgentSession.creator_id == user.id,
                    CreatorAgentSession.plan_item_id.in_(item_ids),
                )
                .order_by(
                    CreatorAgentSession.updated_at.desc(), CreatorAgentSession.created_at.desc()
                )
            )
        )
        .scalars()
        .all()
    )
    session_by_item: dict[uuid.UUID, CreatorAgentSession] = {}
    for session in sessions:
        session_by_item.setdefault(session.plan_item_id, session)
    missing = [item_id for item_id in item_ids if item_id not in session_by_item]
    if missing:
        raise HTTPException(
            status_code=409, detail="Every deliverable must retain a Creator session"
        )
    epoch = int(plan.ownership_epoch or 0)
    if any(int(session_by_item[item_id].ownership_epoch) != epoch for item_id in item_ids):
        raise HTTPException(status_code=409, detail="Creator session ownership epoch is stale")
    jobs = (
        (
            await db.execute(
                select(Job).where(
                    Job.id.in_(
                        [s.target_job_id for s in session_by_item.values() if s.target_job_id]
                    )
                )
            )
        )
        .scalars()
        .all()
        if any(s.target_job_id for s in session_by_item.values())
        else []
    )
    jobs_by_id = {job.id: job for job in jobs}
    for session in session_by_item.values():
        if session.target_job_id:
            job = jobs_by_id.get(session.target_job_id)
            if (
                job is None
                or job.user_id != user.id
                or job.content_plan_item_id != session.plan_item_id
                or int(job.content_plan_ownership_epoch or -1) != epoch
            ):
                raise HTTPException(status_code=409, detail="Creator Job ownership is stale")

    receipt = CreatorWorkspaceReceipt(
        creator_id=user.id,
        plan_id=plan.id,
        ownership_epoch=epoch,
        idempotency_key=body.idempotency_key,
        request_digest=digest,
        status="pending",
    )
    db.add(receipt)
    await db.flush()
    for position, item_id in enumerate(item_ids):
        session = session_by_item[item_id]
        job = jobs_by_id.get(session.target_job_id) if session.target_job_id else None
        generation_receipt = (
            {
                "job_id": str(session.target_job_id),
                "variant_id": session.target_variant_id,
                "render_generation_id": session.target_generation_id,
                "ownership_epoch": epoch,
                "session_revision": int(session.revision),
            }
            if session.target_job_id and session.target_variant_id and session.target_generation_id
            else None
        )
        receipt.deliverables.append(
            CreatorWorkspaceDeliverable(
                creator_id=user.id,
                plan_id=plan.id,
                plan_item_id=item_id,
                creator_session_id=session.id,
                ownership_epoch=epoch,
                session_revision=int(session.revision),
                job_id=job.id if job else None,
                variant_id=session.target_variant_id,
                render_generation_id=session.target_generation_id,
                generation_receipt=generation_receipt,
                status="pending",
                position=position,
            )
        )
    await db.commit()
    await db.refresh(receipt)
    return await _workspace_response(db, receipt, plan)


@router.get(
    "/{plan_id}/workspace/receipts/{receipt_id}",
    response_model=WorkspaceReceiptResponse,
)
async def poll_workspace_receipt(
    plan_id: str,
    receipt_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> WorkspaceReceiptResponse:
    _require_coordination_enabled()
    plan = await _owned_plan(plan_id, user.id, db)
    return await _latest_workspace_receipt(plan, user, db, receipt_id)


@router.get("/{plan_id}/workspace", response_model=WorkspaceReceiptResponse)
@router.get("/{plan_id}/workspace/coordination", response_model=WorkspaceReceiptResponse)
async def poll_latest_workspace_receipt(
    plan_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> WorkspaceReceiptResponse:
    _require_coordination_enabled()
    plan = await _owned_plan(plan_id, user.id, db)
    return await _latest_workspace_receipt(plan, user, db)


async def _refresh_plan_preference_summary(db: AsyncSession, user_id: uuid.UUID) -> str:
    count_rows = (
        await db.execute(
            select(VideoFeedback.signal, func.count())
            .where(VideoFeedback.user_id == user_id)
            .group_by(VideoFeedback.signal)
        )
    ).all()
    notes = (
        (
            await db.execute(
                select(VideoFeedback.note)
                .where(VideoFeedback.user_id == user_id, VideoFeedback.signal == "note")
                .order_by(VideoFeedback.created_at.desc())
                .limit(MAX_NOTES_IN_SUMMARY)
            )
        )
        .scalars()
        .all()
    )
    return build_preference_summary(
        signal_counts={str(signal): int(count) for signal, count in count_rows},
        recent_notes=[str(note) for note in notes if note],
    )


@router.post(
    "/{plan_id}/workspace/preference-signals",
    response_model=WorkspacePreferenceSignalResponse,
    status_code=status.HTTP_201_CREATED,
)
async def record_workspace_preference_signal(
    plan_id: str,
    body: WorkspacePreferenceSignalBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> WorkspacePreferenceSignalResponse:
    """Record only creator-authored feedback and an optional explicit style edit."""

    _require_coordination_enabled()
    plan = await _owned_plan(plan_id, user.id, db, for_update=True)
    epoch = int(plan.ownership_epoch or 0)
    digest = _preference_request_digest(plan.id, epoch, body.note, body.style_edit)
    existing = (
        await db.execute(
            select(CreatorWorkspacePreferenceSignal).where(
                CreatorWorkspacePreferenceSignal.creator_id == user.id,
                CreatorWorkspacePreferenceSignal.plan_id == plan.id,
                CreatorWorkspacePreferenceSignal.client_event_id == body.client_event_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.request_digest != digest:
            raise HTTPException(
                status_code=409, detail="Preference event reused with different content"
            )
        persona = (
            await db.execute(select(Persona).where(Persona.user_id == user.id))
        ).scalar_one_or_none()
        return WorkspacePreferenceSignalResponse(
            signal_id=str(existing.id),
            creator_id=str(user.id),
            plan_id=str(plan.id),
            ownership_epoch=int(existing.ownership_epoch),
            source="creator_explicit",
            note=existing.note,
            style=dict(persona.style) if persona and persona.style else None,
            preference_summary=plan.preference_summary,
        )

    style: dict | None = None
    receipt_id: uuid.UUID | None = None
    if body.receipt_id is not None:
        try:
            receipt_id = uuid.UUID(body.receipt_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="bad receipt id") from exc
        receipt = (
            await db.execute(
                select(CreatorWorkspaceReceipt).where(
                    CreatorWorkspaceReceipt.id == receipt_id,
                    CreatorWorkspaceReceipt.plan_id == plan.id,
                    CreatorWorkspaceReceipt.creator_id == user.id,
                )
            )
        ).scalar_one_or_none()
        if receipt is None:
            raise HTTPException(status_code=404, detail="Workspace receipt not found")
        if int(receipt.ownership_epoch) != epoch:
            raise HTTPException(
                status_code=409, detail="Workspace receipt ownership epoch is stale"
            )

    if body.style_edit is not None:
        if not settings.user_style_enabled:
            raise HTTPException(status_code=404, detail="style_not_enabled")
        persona = (
            await db.execute(select(Persona).where(Persona.user_id == user.id).with_for_update())
        ).scalar_one_or_none()
        if persona is None:
            raise HTTPException(status_code=404, detail="Persona not found")
        raw = dict(persona.style or {})
        edit = body.style_edit.model_dump(mode="json", exclude_none=True)
        for key, value in edit.items():
            if key == "knobs":
                raw["knobs"] = {**dict(raw.get("knobs") or {}), **value}
            else:
                raw[key] = value
        raw["status"] = "edited"
        style = UserStyle.model_validate(raw).model_dump(mode="json")
        persona.style = style

    signal = CreatorWorkspacePreferenceSignal(
        creator_id=user.id,
        plan_id=plan.id,
        receipt_id=receipt_id,
        ownership_epoch=epoch,
        client_event_id=body.client_event_id,
        request_digest=digest,
        source="creator_explicit",
        signal="note",
        note=body.note,
        style_edit=body.style_edit.model_dump(mode="json", exclude_none=True)
        if body.style_edit
        else None,
    )
    db.add(signal)
    db.add(
        VideoFeedback(
            user_id=user.id,
            content_plan_id=plan.id,
            signal="note",
            note=body.note,
        )
    )
    summary = await _refresh_plan_preference_summary(db, user.id)
    plan.preference_summary = summary or None
    await db.commit()
    return WorkspacePreferenceSignalResponse(
        signal_id=str(signal.id),
        creator_id=str(user.id),
        plan_id=str(plan.id),
        ownership_epoch=epoch,
        source="creator_explicit",
        note=body.note,
        style=style,
        preference_summary=plan.preference_summary,
    )


__all__ = [
    "WorkspaceCreateBody",
    "WorkspaceDecisionBody",
    "WorkspaceProposalResponse",
    "WorkspaceReceiptCreateBody",
    "WorkspaceReceiptResponse",
    "WorkspacePreferenceSignalBody",
    "WorkspacePreferenceSignalResponse",
    "router",
]
