"""Bounded, owned catalogue selections. Never accepts a Photos identifier or bytes."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CollectionMember(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_id: UUID
    source_id: UUID
    group: str | None = Field(default=None, min_length=1, max_length=80)


class CollectionWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=160)
    expected_revision: int = Field(ge=0)
    members: list[CollectionMember] = Field(max_length=50)

    @model_validator(mode="after")
    def unique_members(self):
        if len({member.asset_id for member in self.members}) != len(self.members):
            raise ValueError("Each catalogue asset may appear only once")
        if not self.name.strip():
            raise ValueError("Name must not be blank")
        return self


class CollectionOut(BaseModel):
    id: UUID
    name: str
    revision: int
    members: list[CollectionMember]
    unavailable_asset_ids: list[UUID] = Field(default_factory=list)


class CollectionIndexBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    offset: int = Field(default=0, ge=0, le=50)
    # Metadata batch measured with the native 53-asset mixed collection.
    limit: int = Field(default=32, ge=1, le=32)


class CollectionIndexOut(BaseModel):
    revision: int
    next_offset: int | None
    indexed_sources: int
    missing_analysis_asset_ids: list[UUID]
    unavailable_asset_ids: list[UUID]
    truncated_asset_ids: list[UUID]
    untimed_asset_ids: list[UUID] = Field(default_factory=list)


class MediaMomentOut(BaseModel):
    id: UUID
    asset_id: UUID
    source_id: UUID
    generation: str
    start_s: float
    end_s: float
    kind: Literal["visual", "speech"]
    text: str
    uncertain: bool
    evidence: dict


class MomentSearchOut(BaseModel):
    revision: int
    method: Literal["text-evidence-v1"] = "text-evidence-v1"
    moments: list[MediaMomentOut]
    unavailable_asset_ids: list[UUID]
    unindexed_asset_ids: list[UUID] = Field(default_factory=list)
    incomplete_speech_asset_ids: list[UUID] = Field(default_factory=list)
    truncated_asset_ids: list[UUID] = Field(default_factory=list)
    candidate_limit_reached: bool = False
