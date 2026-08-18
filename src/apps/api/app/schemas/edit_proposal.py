"""Versioned, reviewable guided-edit proposal contract.

The proposal is deliberately stored as one JSONB envelope on ``PlanItem``.
Draft and approved snapshots live together so media changes can mark a plan
stale without erasing the creator's last approval.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

ProposalStatus = Literal[
    "briefing",
    "analyzing",
    "drafting",
    "draft",
    "approved",
    "stale",
    "failed",
]
ProposalDirection = Literal["guided_story", "fast_montage", "text_explainer"]
ProposalPace = Literal["relaxed", "balanced", "fast"]
OutputOrientation = Literal["portrait", "landscape"]
MediaLane = Literal["clip", "asset"]
MediaKind = Literal["image", "video"]
BeatLayout = Literal["fullscreen", "supporting_card"]
ThoughtSource = Literal["ai_draft", "user"]
ConversationRole = Literal["user", "agent"]
ConversationPhase = Literal["briefing", "review"]
ConversationSuggestion = Annotated[str, Field(min_length=1, max_length=100)]
EDIT_CONVERSATION_MAX_TURNS = 20
# Who/what approved a proposal — "auto" for AI-designs-by-default
# (GUIDED_AUTO_DESIGN_ENABLED); "user" for an explicit creator approval.
ApprovalMode = Literal["user", "auto"]


class MediaRef(BaseModel):
    """One exact media identity from either existing storage lane."""

    lane: MediaLane
    media_id: str = Field(min_length=1, max_length=100)
    gcs_path: str = Field(min_length=1)
    generation: str = Field(min_length=1)
    kind: MediaKind
    source_filename: str = ""
    duration_s: float | None = Field(default=None, gt=0)
    aspect: float | None = Field(default=None, gt=0)
    content_hash: str | None = None
    user_context: str = ""
    analysis: dict = Field(default_factory=dict)


class StoryBeat(BaseModel):
    beat_id: str = Field(min_length=1, max_length=100)
    topic: str = Field(min_length=1, max_length=80)
    thought: str = Field(default="", max_length=280)
    thought_source: ThoughtSource = "ai_draft"
    media_ids: list[str] = Field(min_length=1, max_length=4)
    layout: BeatLayout = "fullscreen"
    duration_s: float = Field(ge=1.0, le=12.0)


class EditProposalSnapshot(BaseModel):
    direction: ProposalDirection = "guided_story"
    goal: str = Field(default="", max_length=500)
    pace: ProposalPace = "balanced"
    # No artificial floor — see ProposalBrief.duration_s.
    duration_s: int = Field(ge=3, le=60)
    title: str = Field(min_length=1, max_length=100)
    media: list[MediaRef] = Field(min_length=1, max_length=60)
    story_beats: list[StoryBeat] = Field(min_length=1, max_length=20)
    output_orientation: OutputOrientation | None = None
    output_orientation_reason: str = Field(default="", max_length=240)

    @model_validator(mode="after")
    def validate_beat_media(self) -> EditProposalSnapshot:
        known = {m.media_id for m in self.media}
        if len(known) != len(self.media):
            raise ValueError("proposal media IDs must be unique")
        for beat in self.story_beats:
            missing = set(beat.media_ids) - known
            if missing:
                raise ValueError(f"beat {beat.beat_id} references missing media IDs")
        if len({b.beat_id for b in self.story_beats}) != len(self.story_beats):
            raise ValueError("story beat IDs must be unique")
        if self.output_orientation is None:
            orientation, reason = infer_story_output_orientation(self)
            self.output_orientation = orientation
            self.output_orientation_reason = reason
        elif not self.output_orientation_reason:
            self.output_orientation_reason = "The creator selected this output format."
        return self


def _media_aspect(ref: MediaRef) -> float | None:
    """Return analyzed display width/height without guessing from filenames."""

    if ref.aspect is not None and math.isfinite(ref.aspect) and ref.aspect > 0:
        return float(ref.aspect)
    analysis = ref.analysis if isinstance(ref.analysis, dict) else {}
    try:
        width = float(analysis.get("width") or 0)
        height = float(analysis.get("height") or 0)
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return width / height


def infer_story_output_orientation(
    snapshot: EditProposalSnapshot,
) -> tuple[OutputOrientation, str]:
    """Choose a canvas from approved story exposure, not unused uploaded media.

    Each beat's approved duration is split evenly across its selected sources.
    Landscape (>1.05) and portrait (<0.95) exposure vote by that duration;
    near-square sources are neutral. A tie follows the first selected non-square
    source so the opening remains the deterministic creative tie-breaker.
    """

    by_id = {ref.media_id: ref for ref in snapshot.media}
    landscape_s = 0.0
    portrait_s = 0.0
    first_non_square: OutputOrientation | None = None
    usable = 0
    for beat in snapshot.story_beats:
        weight = float(beat.duration_s) / len(beat.media_ids)
        for media_id in beat.media_ids:
            aspect = _media_aspect(by_id[media_id])
            if aspect is None or 0.95 <= aspect <= 1.05:
                continue
            orientation: OutputOrientation = "landscape" if aspect > 1.05 else "portrait"
            first_non_square = first_non_square or orientation
            usable += 1
            if orientation == "landscape":
                landscape_s += weight
            else:
                portrait_s += weight
    if landscape_s > portrait_s:
        selected: OutputOrientation = "landscape"
    elif portrait_s > landscape_s:
        selected = "portrait"
    elif first_non_square is not None:
        selected = first_non_square
    else:
        selected = "portrait"
    reason = (
        f"Auto-selected {selected} from approved story media: "
        f"{landscape_s:.1f}s landscape, {portrait_s:.1f}s portrait; "
        f"{usable} non-square source selections."
    )
    if usable == 0:
        reason = (
            "Auto-selected portrait because the approved story has no usable "
            "non-square aspect metadata."
        )
    return selected, reason


class ApprovedProposalSnapshot(BaseModel):
    proposal_version: int = Field(ge=1)
    media_digest: str = Field(min_length=64, max_length=64)
    approved_at: datetime
    snapshot: EditProposalSnapshot
    # Recorded distinctly from the envelope's mutable EditProposal.approval_mode
    # (which a later reservation can overwrite) so an approved-and-rendered
    # story permanently remembers whether a human or the auto-design flow
    # approved it. None = legacy approvals predating this field (treat as "user").
    approval_mode: ApprovalMode | None = None


class ProposalFailure(BaseModel):
    code: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=500)
    retryable: bool = True
    # Admin/debug-only diagnostic (exception type + short reason). Never shown
    # to end users — _edit_proposal_response() strips this key before the
    # public PlanItem response is built (routes/plan_items.py).
    detail: str | None = Field(default=None, max_length=2000)


class ProposalBrief(BaseModel):
    direction: ProposalDirection = "guided_story"
    goal: str = Field(default="", max_length=500)
    pace: ProposalPace = "balanced"
    # No artificial floor: the planner adapts the story length to whatever
    # footage is actually available (draft_edit_proposal clamps this against
    # analyzed media before it reaches the agent). See agents/DECISIONS.md.
    duration_s: int = Field(default=24, ge=3, le=60)


class EditConversationTurn(BaseModel):
    """One durable turn in the edit-direction conversation."""

    role: ConversationRole
    phase: ConversationPhase = "briefing"
    content: str = Field(min_length=1, max_length=1000)
    suggestions: list[ConversationSuggestion] = Field(default_factory=list, max_length=3)


class EditConversationAttempt(BaseModel):
    """Short-lived single-flight fence around one paid edit-guide call."""

    token: str = Field(min_length=1, max_length=100)
    expected_proposal_version: int = Field(ge=0)
    reserved_proposal_version: int = Field(ge=1)
    started_at: datetime
    placeholder: bool = False


class EditProposal(BaseModel):
    schema_version: Literal[1] = 1
    proposal_version: int = Field(ge=1)
    generation_attempt_id: str = Field(min_length=1, max_length=100)
    media_digest: str | None = Field(default=None, min_length=64, max_length=64)
    status: ProposalStatus
    # Who/what approved this attempt — "auto" for AI-designs-by-default
    # (GUIDED_AUTO_DESIGN_ENABLED); None/"user" for an explicit creator
    # approval. Set when the attempt is reserved (begin_proposal_attempt) and
    # carried through to ApprovedProposalSnapshot.approval_mode on approval.
    approval_mode: ApprovalMode | None = None
    brief: ProposalBrief = Field(default_factory=ProposalBrief)
    conversation: list[EditConversationTurn] = Field(
        default_factory=list, max_length=EDIT_CONVERSATION_MAX_TURNS
    )
    brief_ready: bool = False
    conversation_attempt: EditConversationAttempt | None = None
    draft: EditProposalSnapshot | None = None
    last_approved: ApprovedProposalSnapshot | None = None
    failure: ProposalFailure | None = None


class MediaRefResponse(MediaRef):
    """Media identity plus its short-lived, response-only preview URL."""

    preview_url: str | None = None


class EditProposalSnapshotResponse(EditProposalSnapshot):
    media: list[MediaRefResponse] = Field(min_length=1, max_length=60)


class ApprovedProposalSnapshotResponse(ApprovedProposalSnapshot):
    snapshot: EditProposalSnapshotResponse


class EditProposalResponse(EditProposal):
    """OpenAPI-visible proposal envelope returned by plan-item endpoints."""

    # Attempt tokens are internal write fences. Responses expose only safe UI
    # state so a browser can resume after a reload without learning the token.
    conversation_attempt: None = None
    conversation_in_progress: bool = False
    conversation_retry_required: bool = False
    draft: EditProposalSnapshotResponse | None = None
    last_approved: ApprovedProposalSnapshotResponse | None = None


def canonical_media_digest(media: list[MediaRef]) -> str:
    """Hash only immutable media identities; editorial order is not media state."""

    identities = sorted(
        (
            {
                "lane": ref.lane,
                "media_id": ref.media_id,
                "gcs_path": ref.gcs_path,
                "generation": ref.generation,
                "kind": ref.kind,
                "content_hash": ref.content_hash or "",
            }
            for ref in media
        ),
        key=lambda row: (row["lane"], row["media_id"]),
    )
    payload = json.dumps(identities, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def parse_edit_proposal(value: object) -> EditProposal | None:
    """Fail closed for legacy/corrupt JSONB instead of breaking item reads."""

    if not isinstance(value, dict):
        return None
    try:
        return EditProposal.model_validate(value)
    except Exception:  # noqa: BLE001 - corrupted JSONB is treated as no proposal
        return None
