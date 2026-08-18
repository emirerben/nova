"""Admin triage endpoint for a single plan item.

GET /admin/plan-items/{item_id}/debug — durable, read-only snapshot of a
plan item's state: core fields, clip pool, linked jobs, and a REDACTED
edit_proposal envelope.

Built after a real incident where plan item 85d1de16-ba11-4533-9290-
927a45819cd3 was wedged with edit_proposal.status == "failed" and had to be
triaged with raw SQL over SSH. See docs/runbooks/guided-edit-triage.md.

Auth: X-Admin-Token header (same gate as the rest of admin.py) — reused via
app.routes.admin._require_admin, not duplicated here.

This is admin-only and read-only: no user scoping, no writes. Every field
that can carry the creator's own typed words is either omitted or reduced
to a structural summary before it reaches this response:
  - `edit_proposal.conversation` — each turn becomes
    {role, phase, length, has_suggestions}, never the actual content.
  - `edit_proposal.brief.goal` / `draft` / `last_approved` — reduced to
    length/counts (goal_length, beat_count, media_count, duration_s).
  - `clip_assignments[*].user_note` — omitted entirely (creator-authored
    free text attached to a clip).
  - `edit_proposal_attempt.token` — never returned (internal write fence,
    see schemas/edit_proposal.py); only has_conversation_attempt + its
    started_at/versions survive.
So this endpoint never leaks creator text into logs, admin screenshots, or
a support ticket, even though it is admin-only.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.database import get_db
from app.models import Job, PlanItem, PlanItemAsset
from app.routes.admin import _require_admin
from app.routes.plan_items import derive_item_status
from app.schemas.edit_proposal import parse_edit_proposal

router = APIRouter()

# Same defer set as GET /admin/jobs (app/routes/admin_jobs.py list_jobs): these
# JSONB columns can be multi-megabyte per row and this endpoint never surfaces
# them — only id/status/mode/created_at/failure fields are read below.
_JOB_LIST_DEFERS = (
    defer(Job.assembly_plan),
    defer(Job.probe_metadata),
    defer(Job.transcript),
    defer(Job.scene_cuts),
    defer(Job.all_candidates),
    defer(Job.phase_log),
    defer(Job.pipeline_trace),
)


# ── Response schemas ─────────────────────────────────────────────────────────


class ItemCorePayload(BaseModel):
    id: str
    item_status: str
    edit_format: str
    content_mode: str | None
    montage_preset: str
    created_at: datetime
    updated_at: datetime
    current_job_id: str | None
    has_voiceover: bool
    voiceover_gcs_path: str | None
    scheduled_date: str | None


class ClipGcsPathsPayload(BaseModel):
    count: int
    paths: list[str]


class ClipAssignmentSummaryPayload(BaseModel):
    """Deliberately omits `user_note` — creator-authored free text."""

    media_id: str | None
    gcs_path: str
    kind: str | None
    duration_s: float | None
    aspect: float | None
    generation: str | None
    has_analysis: bool
    analysis_version: Any = None


class PoolAssetPayload(BaseModel):
    id: str
    kind: str
    status: str
    error_code: str | None
    error_detail: str | None
    duration_s: float | None
    aspect: float | None
    source_filename: str | None
    analysis_attempt_count: int
    created_at: datetime


class ItemJobPayload(BaseModel):
    id: str
    status: str
    mode: str | None
    created_at: datetime
    failure_reason: str | None
    error_detail: str | None


class ProposalBriefPayload(BaseModel):
    direction: str
    # `goal` is free-text creator/story direction — in the review phase
    # plan_items.py copies it straight from the review snapshot's goal, so
    # it can carry the creator's own words. Never returned verbatim, only
    # its length, so the UI can show "brief set" without leaking text.
    goal_length: int
    pace: str
    duration_s: int


class ProposalFailurePayload(BaseModel):
    code: str
    message: str
    retryable: bool


class ConversationAttemptPayload(BaseModel):
    """Attempt-fence summary. `token` is an internal write fence (see
    schemas/edit_proposal.py) and is deliberately never returned."""

    has_conversation_attempt: bool
    expected_proposal_version: int | None = None
    reserved_proposal_version: int | None = None
    started_at: datetime | None = None
    placeholder: bool | None = None


class LastApprovedSummaryPayload(BaseModel):
    proposal_version: int
    media_digest: str
    approved_at: datetime
    beat_count: int
    media_count: int


class DraftSummaryPayload(BaseModel):
    beat_count: int
    duration_s: int


class ConversationTurnSummaryPayload(BaseModel):
    """Redacted turn — never the creator's actual typed content."""

    role: str
    phase: str
    length: int
    has_suggestions: bool


class EditProposalDebugPayload(BaseModel):
    status: str
    proposal_version: int
    schema_version: int
    brief: ProposalBriefPayload
    brief_ready: bool
    generation_attempt_id: str
    conversation_attempt: ConversationAttemptPayload
    media_digest: str | None
    failure: ProposalFailurePayload | None
    last_approved: LastApprovedSummaryPayload | None
    draft: DraftSummaryPayload | None
    conversation: list[ConversationTurnSummaryPayload]


class PlanItemDebugResponse(BaseModel):
    item: ItemCorePayload
    clip_gcs_paths: ClipGcsPathsPayload
    clip_assignments: list[ClipAssignmentSummaryPayload]
    pool_assets: list[PoolAssetPayload]
    jobs: list[ItemJobPayload]
    edit_proposal: EditProposalDebugPayload | None
    # Set when PlanItem.edit_proposal is a non-empty dict that failed
    # EditProposal validation (corrupted/legacy JSONB — parse_edit_proposal
    # fails closed and returns None). Surfaces that there IS an envelope an
    # operator should know about, without ever emitting its values — only
    # the top-level key names, which are schema field names, not content.
    edit_proposal_unparseable: bool = False
    edit_proposal_raw_keys: list[str] | None = None


# ── Helpers ──────────────────────────────────────────────────────────────────


def _clip_assignment_summary(raw: dict) -> ClipAssignmentSummaryPayload:
    analysis = raw.get("analysis") if isinstance(raw.get("analysis"), dict) else None
    return ClipAssignmentSummaryPayload(
        media_id=raw.get("media_id"),
        gcs_path=str(raw.get("gcs_path") or ""),
        kind=raw.get("kind"),
        duration_s=raw.get("duration_s"),
        aspect=raw.get("aspect"),
        generation=raw.get("generation"),
        has_analysis=bool(analysis),
        analysis_version=(analysis or {}).get("analysis_version"),
    )


def _edit_proposal_debug_payload(raw: Any) -> EditProposalDebugPayload | None:
    proposal = parse_edit_proposal(raw)
    if proposal is None:
        return None

    last_approved = None
    if proposal.last_approved is not None:
        last_approved = LastApprovedSummaryPayload(
            proposal_version=proposal.last_approved.proposal_version,
            media_digest=proposal.last_approved.media_digest,
            approved_at=proposal.last_approved.approved_at,
            beat_count=len(proposal.last_approved.snapshot.story_beats),
            media_count=len(proposal.last_approved.snapshot.media),
        )

    draft = None
    if proposal.draft is not None:
        draft = DraftSummaryPayload(
            beat_count=len(proposal.draft.story_beats),
            duration_s=proposal.draft.duration_s,
        )

    if proposal.conversation_attempt is not None:
        conversation_attempt = ConversationAttemptPayload(
            has_conversation_attempt=True,
            expected_proposal_version=proposal.conversation_attempt.expected_proposal_version,
            reserved_proposal_version=proposal.conversation_attempt.reserved_proposal_version,
            started_at=proposal.conversation_attempt.started_at,
            placeholder=proposal.conversation_attempt.placeholder,
        )
    else:
        conversation_attempt = ConversationAttemptPayload(has_conversation_attempt=False)

    failure = None
    if proposal.failure is not None:
        failure = ProposalFailurePayload(
            code=proposal.failure.code,
            message=proposal.failure.message,
            retryable=proposal.failure.retryable,
        )

    return EditProposalDebugPayload(
        status=proposal.status,
        proposal_version=proposal.proposal_version,
        schema_version=proposal.schema_version,
        brief=ProposalBriefPayload(
            direction=proposal.brief.direction,
            goal_length=len(proposal.brief.goal),
            pace=proposal.brief.pace,
            duration_s=proposal.brief.duration_s,
        ),
        brief_ready=proposal.brief_ready,
        generation_attempt_id=proposal.generation_attempt_id,
        conversation_attempt=conversation_attempt,
        media_digest=proposal.media_digest,
        failure=failure,
        last_approved=last_approved,
        draft=draft,
        conversation=[
            ConversationTurnSummaryPayload(
                role=turn.role,
                phase=turn.phase,
                length=len(turn.content),
                has_suggestions=bool(turn.suggestions),
            )
            for turn in proposal.conversation
        ],
    )


# ── Endpoint ─────────────────────────────────────────────────────────────────


@router.get("/{item_id}/debug", response_model=PlanItemDebugResponse)
async def get_plan_item_debug(
    item_id: str,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(_require_admin),
) -> PlanItemDebugResponse:
    """Return the full triage snapshot for one plan item.

    404 on both an unknown item id AND a malformed (non-UUID) id — this is
    an operator-facing tool where the id is typically pasted from a Slack
    thread or a job's content_plan_item_id, so "not found" reads the same
    either way rather than surfacing a 400.
    """
    try:
        item_uuid = uuid.UUID(item_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan item not found")

    item_res = await db.execute(select(PlanItem).where(PlanItem.id == item_uuid))
    item = item_res.scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan item not found")

    assets_res = await db.execute(
        select(PlanItemAsset)
        .options(defer(PlanItemAsset.analysis))
        .where(PlanItemAsset.plan_item_id == item_uuid)
        .order_by(PlanItemAsset.created_at)
    )
    assets = list(assets_res.scalars().all())

    # Same linkage predicate as GET /admin/jobs?content_plan_item_id=
    # (app/routes/admin_jobs.py list_jobs) — kept in sync deliberately so this
    # endpoint and the jobs list agree on which jobs "belong" to an item.
    # Deferring the heavy JSONB columns here mirrors that endpoint's list
    # query — this route never surfaces them, only id/status/mode/
    # created_at/failure fields.
    jobs_res = await db.execute(
        select(Job)
        .options(*_JOB_LIST_DEFERS)
        .where(Job.content_plan_item_id == item_uuid)
        .order_by(Job.created_at.desc())
    )
    jobs = list(jobs_res.scalars().all())

    # Avoid a second, fully-hydrated Job fetch (via PlanItem.current_job)
    # just to read one status string: reuse the row already fetched above
    # by content_plan_item_id linkage. Falls back to None (item.item_status
    # wins in derive_item_status) on the rare drift where current_job_id
    # points at a job whose content_plan_item_id doesn't match this item —
    # cross-check the `jobs` array in the response in that case.
    current_job_row = (
        next((j for j in jobs if j.id == item.current_job_id), None)
        if item.current_job_id
        else None
    )
    item_status = derive_item_status(
        SimpleNamespace(item_status=item.item_status, current_job=current_job_row)
    )

    edit_proposal_payload = _edit_proposal_debug_payload(item.edit_proposal)
    edit_proposal_unparseable = False
    edit_proposal_raw_keys: list[str] | None = None
    if (
        edit_proposal_payload is None
        and isinstance(item.edit_proposal, dict)
        and item.edit_proposal
    ):
        edit_proposal_unparseable = True
        edit_proposal_raw_keys = sorted(item.edit_proposal.keys())

    scheduled_date = getattr(item, "scheduled_date", None)

    return PlanItemDebugResponse(
        item=ItemCorePayload(
            id=str(item.id),
            item_status=item_status,
            edit_format=item.edit_format,
            content_mode=item.content_mode,
            montage_preset=item.montage_preset,
            created_at=item.created_at,
            updated_at=item.updated_at,
            current_job_id=str(item.current_job_id) if item.current_job_id else None,
            has_voiceover=bool(item.voiceover_gcs_path),
            voiceover_gcs_path=item.voiceover_gcs_path,
            scheduled_date=scheduled_date.isoformat() if scheduled_date else None,
        ),
        clip_gcs_paths=ClipGcsPathsPayload(
            count=len(item.clip_gcs_paths or []),
            paths=list(item.clip_gcs_paths or []),
        ),
        clip_assignments=[
            _clip_assignment_summary(raw)
            for raw in (item.clip_assignments or [])
            if isinstance(raw, dict)
        ],
        pool_assets=[
            PoolAssetPayload(
                id=str(a.id),
                kind=a.kind,
                status=a.status,
                error_code=a.error_code,
                error_detail=a.error_detail,
                duration_s=a.duration_s,
                aspect=a.aspect,
                source_filename=a.source_filename,
                analysis_attempt_count=a.analysis_attempt_count,
                created_at=a.created_at,
            )
            for a in assets
        ],
        jobs=[
            ItemJobPayload(
                id=str(j.id),
                status=j.status,
                mode=j.mode,
                created_at=j.created_at,
                failure_reason=j.failure_reason,
                error_detail=j.error_detail,
            )
            for j in jobs
        ],
        edit_proposal=edit_proposal_payload,
        edit_proposal_unparseable=edit_proposal_unparseable,
        edit_proposal_raw_keys=edit_proposal_raw_keys,
    )
