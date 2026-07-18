"""Strict, renderer-independent contracts for Smart Captions.

The semantic planner may choose roles, word spans, and closed tokens only.
Milliseconds, coordinates, storage paths, fonts, colors, and arbitrary effects
are resolved later by deterministic policy and preset code.
"""

from __future__ import annotations

import hashlib
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SMART_EDIT_SCHEMA_VERSION = "2026-07-18"
MAX_BASELINE_CAPTION_CUES = 300
MAX_SMART_EDIT_EVENTS = 120
MAX_SMART_WORDS = 600
_WORD_ID_RE = re.compile(r"^w\d{6}$")
_TOKEN_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_ASSET_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
_EVENT_ID_RE = re.compile(r"^[a-f0-9]{24}$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SmartWord(StrictModel):
    word_id: str
    spoken_text: str = Field(min_length=1, max_length=200)
    display_text: str = Field(min_length=1, max_length=200)
    normalized_text: str = Field(min_length=1, max_length=200)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    timing_quality: Literal["aligned", "segment_estimate", "unsafe"]
    display_alignment: list[str] = Field(min_length=1, max_length=8)
    language: str | None = Field(default=None, max_length=16)

    @field_validator("word_id")
    @classmethod
    def validate_word_id(cls, value: str) -> str:
        if not _WORD_ID_RE.fullmatch(value):
            raise ValueError("word_id must match w000001")
        return value

    @field_validator("display_alignment")
    @classmethod
    def validate_display_alignment(cls, values: list[str]) -> list[str]:
        if any(not _WORD_ID_RE.fullmatch(value) for value in values):
            raise ValueError("display_alignment contains an invalid word id")
        if len(values) != len(set(values)):
            raise ValueError("display_alignment word ids must be unique")
        return values

    @model_validator(mode="after")
    def validate_timing(self) -> SmartWord:
        if self.end_ms <= self.start_ms:
            raise ValueError("end_ms must be greater than start_ms")
        return self


class BaselineCaptionCue(StrictModel):
    cue_id: str = Field(min_length=1, max_length=64)
    word_ids: list[str] = Field(min_length=1, max_length=24)
    display_text: str = Field(min_length=1, max_length=500)

    @field_validator("word_ids")
    @classmethod
    def validate_word_ids(cls, values: list[str]) -> list[str]:
        if any(not _WORD_ID_RE.fullmatch(value) for value in values):
            raise ValueError("caption cue contains an invalid word id")
        if len(values) != len(set(values)):
            raise ValueError("caption cue word ids must be unique")
        return values


SemanticRole = Literal["hook", "context_shift", "list_item", "example", "payoff", "cta"]


class SemanticEventIntent(StrictModel):
    role: SemanticRole
    start_word_id: str
    end_word_id: str
    anchor_word_id: str
    confidence_tier: Literal["high", "medium", "low"]
    lane_tokens: list[str] = Field(default_factory=list, max_length=5)
    asset_intent_tags: list[str] = Field(default_factory=list, max_length=8)
    visual_asset_ids: list[str] = Field(default_factory=list, max_length=5)
    rationale: str = Field(default="", max_length=500)

    @field_validator("start_word_id", "end_word_id", "anchor_word_id")
    @classmethod
    def validate_word_id(cls, value: str) -> str:
        if not _WORD_ID_RE.fullmatch(value):
            raise ValueError("event word ids must match w000001")
        return value

    @field_validator("lane_tokens", "asset_intent_tags")
    @classmethod
    def validate_tokens(cls, values: list[str]) -> list[str]:
        if any(not _TOKEN_RE.fullmatch(value) for value in values):
            raise ValueError("tokens must use the closed lowercase token syntax")
        return values

    @field_validator("visual_asset_ids")
    @classmethod
    def validate_asset_ids(cls, values: list[str]) -> list[str]:
        if any(not _ASSET_ID_RE.fullmatch(value) for value in values):
            raise ValueError("visual asset ids must be registry ids")
        return values

    @model_validator(mode="after")
    def validate_word_span(self) -> SemanticEventIntent:
        if not self.start_word_id <= self.anchor_word_id <= self.end_word_id:
            raise ValueError("event word span must satisfy start <= anchor <= end")
        return self


class EventAnchor(StrictModel):
    word_id: str
    offset_ms: int = Field(default=0, ge=-1000, le=1000)

    @field_validator("word_id")
    @classmethod
    def validate_word_id(cls, value: str) -> str:
        if not _WORD_ID_RE.fullmatch(value):
            raise ValueError("anchor word id must match w000001")
        return value


class CaptionEmphasisLane(StrictModel):
    kind: Literal["caption_emphasis"]
    token: str
    baseline_caption_word_ids: list[str] = Field(min_length=1, max_length=24)

    @field_validator("token")
    @classmethod
    def validate_token(cls, value: str) -> str:
        if not _TOKEN_RE.fullmatch(value):
            raise ValueError("caption token must use the closed token syntax")
        return value

    @field_validator("baseline_caption_word_ids")
    @classmethod
    def validate_word_ids(cls, values: list[str]) -> list[str]:
        if any(not _WORD_ID_RE.fullmatch(value) for value in values):
            raise ValueError("caption emphasis contains an invalid word id")
        return values


class TextLane(StrictModel):
    kind: Literal["text"]
    token: str
    transcript_word_ids: list[str] = Field(min_length=1, max_length=24)
    transform: Literal["verbatim", "list_number_from_sequence", "fixed_preset_token"]
    sequence_number: int | None = Field(default=None, ge=1, le=20)
    claimed_word_ids: list[str] = Field(default_factory=list, max_length=24)
    caption_visibility: Literal["keep", "suppress_claimed_span"] = "keep"

    @field_validator("token")
    @classmethod
    def validate_token(cls, value: str) -> str:
        if not _TOKEN_RE.fullmatch(value):
            raise ValueError("text token must use the closed token syntax")
        return value

    @field_validator("transcript_word_ids", "claimed_word_ids")
    @classmethod
    def validate_word_ids(cls, values: list[str]) -> list[str]:
        if any(not _WORD_ID_RE.fullmatch(value) for value in values):
            raise ValueError("text lane contains an invalid word id")
        return values


class VisualLane(StrictModel):
    kind: Literal["visual"]
    asset_id: str
    zone: str
    entrance_token: str
    exit_policy: Literal["event_end", "group_end", "video_end"] = "event_end"
    composition_group_id: str | None = Field(default=None, max_length=64)
    group_order: int = Field(default=0, ge=0, le=20)
    alternatives: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("asset_id")
    @classmethod
    def validate_asset_id(cls, value: str) -> str:
        if not _ASSET_ID_RE.fullmatch(value):
            raise ValueError("asset_id must be a registry id, not a path")
        return value

    @field_validator("alternatives")
    @classmethod
    def validate_alternatives(cls, values: list[str]) -> list[str]:
        if any(not _ASSET_ID_RE.fullmatch(value) for value in values):
            raise ValueError("visual alternatives must be registry ids")
        return values

    @field_validator("zone", "entrance_token", "composition_group_id")
    @classmethod
    def validate_tokens(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not _TOKEN_RE.fullmatch(value):
            raise ValueError("visual fields must use closed tokens")
        return value


class SfxLane(StrictModel):
    kind: Literal["sfx"]
    role_tokens: list[str] = Field(min_length=1, max_length=3)
    sync_to_event_id: str
    offset_ms: int = Field(ge=-1000, le=1000)
    gain_token: str

    @field_validator("sync_to_event_id")
    @classmethod
    def validate_sync_event_id(cls, value: str) -> str:
        if not _EVENT_ID_RE.fullmatch(value):
            raise ValueError("sync_to_event_id must be a stable Smart event id")
        return value

    @field_validator("gain_token")
    @classmethod
    def validate_gain_token(cls, value: str) -> str:
        if not _TOKEN_RE.fullmatch(value):
            raise ValueError("gain_token must use the closed token syntax")
        return value

    @field_validator("role_tokens")
    @classmethod
    def validate_role_tokens(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("SFX role tokens must be unique")
        if any(not _TOKEN_RE.fullmatch(value) for value in values):
            raise ValueError("SFX role tokens must use the closed token syntax")
        return values


class BoundaryEffectLane(StrictModel):
    kind: Literal["boundary_effect"]
    effect_token: str

    @field_validator("effect_token")
    @classmethod
    def validate_effect_token(cls, value: str) -> str:
        if not _TOKEN_RE.fullmatch(value):
            raise ValueError("effect_token must use the closed token syntax")
        return value


SmartEditLane = Annotated[
    CaptionEmphasisLane | TextLane | VisualLane | SfxLane | BoundaryEffectLane,
    Field(discriminator="kind"),
]


class SmartEditEvent(StrictModel):
    event_id: str
    role: SemanticRole
    start_word_id: str
    end_word_id: str
    anchor: EventAnchor
    active_start_ms: int = Field(ge=0)
    active_end_ms: int = Field(gt=0)
    confidence_tier: Literal["high", "medium", "low"]
    spatial_owner: str | None = Field(default=None, max_length=64)
    enabled: bool = True
    lanes: list[SmartEditLane] = Field(default_factory=list, max_length=5)
    provenance: list[str] = Field(default_factory=list, max_length=16)

    @field_validator("event_id")
    @classmethod
    def validate_event_id(cls, value: str) -> str:
        if not _EVENT_ID_RE.fullmatch(value):
            raise ValueError("event_id must be a 24-character lowercase hex digest")
        return value

    @field_validator("start_word_id", "end_word_id")
    @classmethod
    def validate_word_id(cls, value: str) -> str:
        if not _WORD_ID_RE.fullmatch(value):
            raise ValueError("event word ids must match w000001")
        return value

    @model_validator(mode="after")
    def validate_event(self) -> SmartEditEvent:
        if not self.start_word_id <= self.anchor.word_id <= self.end_word_id:
            raise ValueError("event word span must satisfy start <= anchor <= end")
        if self.active_end_ms <= self.active_start_ms:
            raise ValueError("active_end_ms must be greater than active_start_ms")
        kinds = [lane.kind for lane in self.lanes]
        if len(kinds) != len(set(kinds)):
            raise ValueError("an event may contain at most one lane of each kind")
        return self


class SmartEditPlanDocument(StrictModel):
    schema_version: Literal[SMART_EDIT_SCHEMA_VERSION] = SMART_EDIT_SCHEMA_VERSION
    preset_id: str = Field(default="cigdem", min_length=1, max_length=64)
    preset_version: str = Field(default="v1", min_length=1, max_length=64)
    baseline_captions: list[BaselineCaptionCue] = Field(
        min_length=1, max_length=MAX_BASELINE_CAPTION_CUES
    )
    events: list[SmartEditEvent] = Field(default_factory=list, max_length=MAX_SMART_EDIT_EVENTS)

    @model_validator(mode="after")
    def validate_references(self) -> SmartEditPlanDocument:
        cue_ids = [cue.cue_id for cue in self.baseline_captions]
        if len(cue_ids) != len(set(cue_ids)):
            raise ValueError("baseline caption cue ids must be unique")

        caption_word_ids = [word_id for cue in self.baseline_captions for word_id in cue.word_ids]
        if len(caption_word_ids) != len(set(caption_word_ids)):
            raise ValueError("a baseline word id may belong to only one caption cue")
        caption_words = set(caption_word_ids)

        event_ids = [event.event_id for event in self.events]
        if len(event_ids) != len(set(event_ids)):
            raise ValueError("event ids must be unique")
        known_events = set(event_ids)

        for event in self.events:
            referenced_words = {
                event.start_word_id,
                event.end_word_id,
                event.anchor.word_id,
            }
            for lane in event.lanes:
                if isinstance(lane, CaptionEmphasisLane):
                    referenced_words.update(lane.baseline_caption_word_ids)
                elif isinstance(lane, TextLane):
                    referenced_words.update(lane.transcript_word_ids)
                elif isinstance(lane, SfxLane):
                    if lane.sync_to_event_id not in known_events:
                        raise ValueError("SFX lane references an unknown Smart event")
                    if lane.sync_to_event_id != event.event_id:
                        raise ValueError("SFX lane must synchronize to its containing Smart event")
            if not referenced_words <= caption_words:
                raise ValueError("event references words outside baseline captions")
        return self


def build_event_id(
    *,
    preset_version: str,
    role: SemanticRole,
    start_word_id: str,
    end_word_id: str,
    collision_ordinal: int,
) -> str:
    """Return the stable event identity owned by deterministic policy code."""

    if collision_ordinal < 0:
        raise ValueError("collision_ordinal must be non-negative")
    material = "|".join((preset_version, role, start_word_id, end_word_id, str(collision_ordinal)))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
