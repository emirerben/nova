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
  - `edit_proposal.brief.goal` / `creator_request` / `draft` / `last_approved`
    — reduced to length/counts (goal_length, creator_request_length,
    beat_count, media_count, duration_s).
  - `clip_assignments[*].user_note` — omitted entirely (creator-authored
    free text attached to a clip).
  - `edit_proposal_attempt.token` — never returned (internal write fence,
    see schemas/edit_proposal.py); only has_conversation_attempt + its
    started_at/versions survive.
So this endpoint never leaks creator text into logs, admin screenshots, or
a support ticket, even though it is admin-only.

PUT/GET/DELETE /admin/plan-items/{item_id}/phone-lanes (KRI-174 Phase 1.5) are
the one WRITE surface in this otherwise read-only file. They let an operator
hand-author a `PhoneSubtitledLaneRequest`
(`app.pipeline.phone_subtitled_lanes`) on a plan item before the prompt-
grounding phase that will eventually author these automatically exists. The
admin passes ASSET IDS (Visuals-pool `PlanItemAsset.id` / sound-effect
catalog ids), never storage paths — PUT resolves each id against the item's
own ready pool rows / the published sound-effect catalog server-side and
persists the fully-resolved request (with `gcs_path`/`generation` filled in)
on `PlanItem.phone_lane_request`. That JSON is private job state: the render
dispatcher copies it verbatim into
`Job.assembly_plan["_phone_subtitled_lanes_v1"]` for the item's NEXT
Generate/Retry, and it is stripped from any public assembly-plan response.
Nothing here takes effect while `PHONE_SUBTITLED_MEDIA_LANES_ENABLED` is off.
"""

from __future__ import annotations

import base64
import uuid
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.config import settings
from app.database import get_db
from app.models import AgentRun, Job, PlanItem, PlanItemAsset, SoundEffect
from app.pipeline.phone_subtitled_lanes import (
    PhoneSubtitledLaneRequest,
    SubtitledEndingClip,
    SubtitledOverlayCard,
    SubtitledSoundEffect,
    sfx_path_is_playable,
)
from app.routes._admin_schemas import AgentRunPayload, agent_run_to_payload
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
    creator_request_length: int
    pace: str
    # ProposalDuration is int | float: a narrated brief carries the
    # voiceover's fractional length, which an int field rejects (500).
    duration_s: float


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
    duration_s: float


class PlannerFallbackPayload(BaseModel):
    """KRI-126: surfaces the deterministic-fallback marker (never shown to
    end users). `reason` may contain a truncated model/exception message —
    admin-only, kept bounded by the source field's own 500-char limit."""

    reason: str
    direction: str
    at: datetime


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
    planner_fallback: PlannerFallbackPayload | None = None


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


class ProposalTraceResponse(BaseModel):
    """Sensitive, admin-only proposal planner trace.

    This intentionally lives apart from the redacted ``/debug`` snapshot:
    raw agent I/O can include model-generated semantic proposal content and
    must only be loaded on an explicit operator diagnostic request.
    """

    item_id: str
    direction: str | None = None
    prompt_version: str | None = None
    planning_diagnostics: dict[str, Any] | None = None
    agent_runs: list[AgentRunPayload]
    agent_runs_has_more: bool = False
    # KRI-186: opaque `(created_at,id)` cursor for the next (older) page; null on
    # the last page. Pass it back as `?cursor=`.
    next_cursor: str | None = None


def _encode_trace_cursor(created_at: datetime, run_id: uuid.UUID) -> str:
    # URL-safe base64 so the value survives a query string ("+" in an ISO offset
    # would otherwise decode as a space).
    raw = f"{created_at.isoformat()}|{run_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_trace_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw_created_at, raw_id = base64.urlsafe_b64decode(padded).decode().rsplit("|", 1)
        return datetime.fromisoformat(raw_created_at), uuid.UUID(raw_id)
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid cursor"
        )


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

    planner_fallback = None
    if proposal.planner_fallback is not None:
        planner_fallback = PlannerFallbackPayload(
            reason=proposal.planner_fallback.reason,
            direction=proposal.planner_fallback.direction,
            at=proposal.planner_fallback.at,
        )

    return EditProposalDebugPayload(
        status=proposal.status,
        proposal_version=proposal.proposal_version,
        schema_version=proposal.schema_version,
        brief=ProposalBriefPayload(
            direction=proposal.brief.direction,
            goal_length=len(proposal.brief.goal),
            creator_request_length=len(proposal.brief.creator_request),
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
        planner_fallback=planner_fallback,
    )


def _proposal_trace_metadata(raw: Any) -> tuple[str | None, str | None, dict[str, Any] | None]:
    """Extract operator diagnostics without coupling this route to a schema rollout.

    ``planning_diagnostics`` was introduced after deployed proposal envelopes
    already existed, so older schema versions simply return ``None`` here.
    """
    proposal = parse_edit_proposal(raw)
    if proposal is None:
        return None, None, None
    diagnostics = getattr(proposal, "planning_diagnostics", None)
    if not isinstance(diagnostics, dict):
        diagnostics = None
    prompt_version = diagnostics.get("prompt_version") if diagnostics else None
    return proposal.brief.direction, prompt_version, diagnostics


# ── Endpoint ─────────────────────────────────────────────────────────────────


@router.get("/{item_id}/proposal-trace", response_model=ProposalTraceResponse)
async def get_plan_item_proposal_trace(
    item_id: str,
    limit: int = Query(default=10, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=128),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(_require_admin),
) -> ProposalTraceResponse:
    """Return latest pre-render planner runs and scheduler diagnostics.

    The cap (``limit``, default 10, max 100) prevents an unusual retry storm from
    returning unbounded raw model output in one response. Rows are newest first
    so rejected/schema-failed semantic output appears immediately to the
    operator. Older runs page through ``cursor`` (the previous response's
    ``next_cursor``, a ``(created_at, id)`` keyset).
    """
    cursor_key = _decode_trace_cursor(cursor) if cursor else None
    try:
        item_uuid = uuid.UUID(item_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan item not found")

    item_res = await db.execute(select(PlanItem).where(PlanItem.id == item_uuid))
    item = item_res.scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan item not found")

    runs_query = select(AgentRun).where(AgentRun.plan_item_id == item_uuid)
    if cursor_key is not None:
        runs_query = runs_query.where(tuple_(AgentRun.created_at, AgentRun.id) < cursor_key)
    runs_res = await db.execute(
        runs_query.order_by(AgentRun.created_at.desc(), AgentRun.id.desc()).limit(limit + 1)
    )
    fetched_runs = list(runs_res.scalars().all())
    page_runs = fetched_runs[:limit]
    has_more = len(fetched_runs) > limit
    direction, prompt_version, diagnostics = _proposal_trace_metadata(item.edit_proposal)
    return ProposalTraceResponse(
        item_id=str(item.id),
        direction=direction,
        prompt_version=prompt_version,
        planning_diagnostics=diagnostics,
        agent_runs=[agent_run_to_payload(run) for run in page_runs],
        agent_runs_has_more=has_more,
        next_cursor=(
            _encode_trace_cursor(page_runs[-1].created_at, page_runs[-1].id)
            if has_more and page_runs
            else None
        ),
    )


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


# ── Phone subtitled lanes (admin write) ─────────────────────────────────────
#
# See the module docstring. The admin-facing request schemas below mirror
# `app.pipeline.phone_subtitled_lanes`'s `Subtitled*` models but take an
# asset id instead of a resolved `gcs_path`/`generation` pin -- the route
# resolves that pin server-side and never trusts a caller-supplied path.

_ADMIN_LANE_ID_PATTERN = r"^\S+$"


class AdminOverlayCard(BaseModel):
    """One overlay card requested over the speaker clip, by asset id."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=80, pattern=_ADMIN_LANE_ID_PATTERN)
    media_id: str = Field(min_length=1, max_length=160, pattern=_ADMIN_LANE_ID_PATTERN)
    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)
    x_frac: float = Field(default=0.5, ge=0, le=1)
    y_frac: float = Field(default=0.4, ge=0, le=1)
    scale: float = Field(default=0.35, ge=0.05, le=1)
    fade: bool = False
    z: int = Field(default=0, ge=0)


class AdminSoundEffect(BaseModel):
    """One catalog sound effect requested at a point on the timeline, by
    catalog id."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=80, pattern=_ADMIN_LANE_ID_PATTERN)
    catalog_id: str = Field(min_length=1, max_length=160, pattern=_ADMIN_LANE_ID_PATTERN)
    at_s: float = Field(ge=0)
    volume: float = Field(default=1.0, ge=0, le=2)


class AdminEndingClip(BaseModel):
    """An optional muted Visuals-pool video appended after the speaker clip,
    by asset id."""

    model_config = ConfigDict(extra="forbid")

    media_id: str = Field(min_length=1, max_length=160, pattern=_ADMIN_LANE_ID_PATTERN)
    trim_start_s: float = Field(default=0.0, ge=0)
    max_duration_s: float | None = Field(default=None, gt=0)


class AdminPhoneLaneRequest(BaseModel):
    """The PUT request body: an unresolved lane request expressed entirely
    in asset ids the route must resolve before it can be stored."""

    model_config = ConfigDict(extra="forbid")

    overlays: list[AdminOverlayCard] = Field(default_factory=list)
    sound_effects: list[AdminSoundEffect] = Field(default_factory=list)
    ending_clip: AdminEndingClip | None = None


class PhoneLanesResponse(BaseModel):
    item_id: str
    phone_lane_request: dict[str, Any] | None
    flag_enabled: bool


class PhoneLanesPutResponse(PhoneLanesResponse):
    note: str


_PHONE_LANES_NOTE = (
    "This lane request applies to the NEXT Generate or Retry of this item, "
    "and only while PHONE_SUBTITLED_MEDIA_LANES_ENABLED is on."
)


async def _load_plan_item_or_404(db: AsyncSession, item_id: str) -> PlanItem:
    """Shared 404 behavior for the phone-lanes endpoints: an unknown id and a
    malformed (non-UUID) id both read as "not found", matching `/debug`."""
    try:
        item_uuid = uuid.UUID(item_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan item not found")

    item_res = await db.execute(select(PlanItem).where(PlanItem.id == item_uuid))
    item = item_res.scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan item not found")
    return item


def _require_bound_asset(
    assets_by_media_id: dict[str, PlanItemAsset],
    item: PlanItem,
    media_id: str,
    *,
    kind: str,
    role: str,
) -> PlanItemAsset:
    """Resolve one requested `media_id` to a ready, in-item, generation-pinned
    `PlanItemAsset` of the required `kind`, or raise a 422 naming `role` and
    `media_id`. Never trusts a caller-supplied path/generation."""
    noun = "image" if kind == "image" else "video"
    asset = assets_by_media_id.get(media_id)
    if (
        asset is None
        or asset.plan_item_id != item.id
        or asset.status != "ready"
        or asset.kind != kind
        or not asset.gcs_generation
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"{role} references media {media_id} which is not a ready "
                f"{noun} in this item's Visuals"
            ),
        )
    return asset


@router.get("/{item_id}/phone-lanes", response_model=PhoneLanesResponse)
async def get_plan_item_phone_lanes(
    item_id: str,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(_require_admin),
) -> PhoneLanesResponse:
    """Read-only: never writes. Returns the stored request verbatim (or None)."""
    item = await _load_plan_item_or_404(db, item_id)
    return PhoneLanesResponse(
        item_id=str(item.id),
        phone_lane_request=item.phone_lane_request,
        flag_enabled=settings.phone_subtitled_media_lanes_enabled,
    )


@router.put("/{item_id}/phone-lanes", response_model=PhoneLanesPutResponse)
async def put_plan_item_phone_lanes(
    item_id: str,
    body: AdminPhoneLaneRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(_require_admin),
) -> PhoneLanesPutResponse:
    """Resolve every asset id + catalog id in `body` and persist the fully
    resolved `PhoneSubtitledLaneRequest` on the item. 422s name the offending
    card/effect and why; 404 matches `/debug` (unknown or malformed id)."""
    item = await _load_plan_item_or_404(db, item_id)

    overlay_ids = [card.id for card in body.overlays]
    if len(overlay_ids) != len(set(overlay_ids)):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="overlay cards must have unique ids within one request",
        )
    sfx_ids = [sfx.id for sfx in body.sound_effects]
    if len(sfx_ids) != len(set(sfx_ids)):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="sound effects must have unique ids within one request",
        )

    media_ids = {card.media_id for card in body.overlays}
    if body.ending_clip is not None:
        media_ids.add(body.ending_clip.media_id)

    assets_by_media_id: dict[str, PlanItemAsset] = {}
    if media_ids:
        parsed_ids: list[uuid.UUID] = []
        for media_id in media_ids:
            try:
                parsed_ids.append(uuid.UUID(media_id))
            except (ValueError, AttributeError, TypeError):
                continue  # unresolved below -> 422 via _require_bound_asset
        if parsed_ids:
            assets_res = await db.execute(
                select(PlanItemAsset).where(PlanItemAsset.id.in_(parsed_ids))
            )
            assets_by_media_id = {str(a.id): a for a in assets_res.scalars().all()}

    overlay_cards: list[SubtitledOverlayCard] = []
    for card in body.overlays:
        asset = _require_bound_asset(
            assets_by_media_id, item, card.media_id, kind="image", role=f"overlay card '{card.id}'"
        )
        try:
            overlay_cards.append(
                SubtitledOverlayCard(
                    id=card.id,
                    media_id=card.media_id,
                    gcs_path=asset.gcs_path,
                    generation=str(asset.gcs_generation),
                    start_s=card.start_s,
                    end_s=card.end_s,
                    x_frac=card.x_frac,
                    y_frac=card.y_frac,
                    scale=card.scale,
                    fade=card.fade,
                    z=card.z,
                )
            )
        except ValidationError as exc:
            reason = exc.errors()[0].get("msg", "invalid")
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"overlay card '{card.id}' is invalid: {reason}",
            ) from exc

    sound_effects: list[SubtitledSoundEffect] = []
    for sfx in body.sound_effects:
        catalog = await db.get(SoundEffect, sfx.catalog_id)
        if (
            catalog is None
            or catalog.published_at is None
            or catalog.archived_at is not None
            or catalog.status != "ready"
            or not catalog.audio_gcs_path
            or not sfx_path_is_playable(catalog.audio_gcs_path)
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"sound effect '{sfx.id}' references catalog_id {sfx.catalog_id} "
                    "which is not a published, playable sound effect"
                ),
            )
        sound_effects.append(
            SubtitledSoundEffect(
                id=sfx.id, catalog_id=sfx.catalog_id, at_s=sfx.at_s, volume=sfx.volume
            )
        )

    ending_clip: SubtitledEndingClip | None = None
    if body.ending_clip is not None:
        asset = _require_bound_asset(
            assets_by_media_id, item, body.ending_clip.media_id, kind="video", role="ending clip"
        )
        ending_clip = SubtitledEndingClip(
            media_id=body.ending_clip.media_id,
            gcs_path=asset.gcs_path,
            generation=str(asset.gcs_generation),
            trim_start_s=body.ending_clip.trim_start_s,
            max_duration_s=body.ending_clip.max_duration_s,
        )

    try:
        lane_request = PhoneSubtitledLaneRequest(
            overlays=overlay_cards,
            sound_effects=sound_effects,
            ending_clip=ending_clip,
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"invalid phone lane request: {exc.errors()[0].get('msg', 'invalid')}",
        ) from exc

    stored = lane_request.model_dump(mode="json")
    item.phone_lane_request = stored
    await db.commit()

    return PhoneLanesPutResponse(
        item_id=str(item.id),
        phone_lane_request=stored,
        flag_enabled=settings.phone_subtitled_media_lanes_enabled,
        note=_PHONE_LANES_NOTE,
    )


@router.delete("/{item_id}/phone-lanes", response_model=PhoneLanesResponse)
async def delete_plan_item_phone_lanes(
    item_id: str,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(_require_admin),
) -> PhoneLanesResponse:
    """Clear any stored lane request. Idempotent: clearing an already-empty
    item still commits and returns the same None shape."""
    item = await _load_plan_item_or_404(db, item_id)
    item.phone_lane_request = None
    await db.commit()
    return PhoneLanesResponse(
        item_id=str(item.id),
        phone_lane_request=None,
        flag_enabled=settings.phone_subtitled_media_lanes_enabled,
    )
