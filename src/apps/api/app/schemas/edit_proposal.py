"""Versioned, reviewable guided-edit proposal contract.

The proposal is deliberately stored as one JSONB envelope on ``PlanItem``.
Draft and approved snapshots live together so media changes can mark a plan
stale without erasing the creator's last approval.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

ProposalStatus = Literal["analyzing", "drafting", "draft", "approved", "stale", "failed"]
ProposalDirection = Literal["guided_story", "fast_montage", "text_explainer"]
ProposalPace = Literal["relaxed", "balanced", "fast"]
MediaLane = Literal["clip", "asset"]
MediaKind = Literal["image", "video"]
BeatLayout = Literal["fullscreen", "supporting_card"]
ThoughtSource = Literal["ai_draft", "user"]


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
    duration_s: int = Field(ge=10, le=60)
    title: str = Field(min_length=1, max_length=100)
    media: list[MediaRef] = Field(min_length=1, max_length=60)
    story_beats: list[StoryBeat] = Field(min_length=1, max_length=20)

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
        return self


class ApprovedProposalSnapshot(BaseModel):
    proposal_version: int = Field(ge=1)
    media_digest: str = Field(min_length=64, max_length=64)
    approved_at: datetime
    snapshot: EditProposalSnapshot


class ProposalFailure(BaseModel):
    code: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=500)
    retryable: bool = True


class ProposalBrief(BaseModel):
    direction: ProposalDirection = "guided_story"
    goal: str = Field(default="", max_length=500)
    pace: ProposalPace = "balanced"
    duration_s: int = Field(default=24, ge=10, le=60)


class EditProposal(BaseModel):
    schema_version: Literal[1] = 1
    proposal_version: int = Field(ge=1)
    generation_attempt_id: str = Field(min_length=1, max_length=100)
    media_digest: str | None = Field(default=None, min_length=64, max_length=64)
    status: ProposalStatus
    brief: ProposalBrief = Field(default_factory=ProposalBrief)
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
