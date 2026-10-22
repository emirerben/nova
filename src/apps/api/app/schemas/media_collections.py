"""Bounded, owned catalogue selections. Never accepts a Photos identifier or bytes."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.research import StoryResearch


class CollectionMember(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_id: UUID
    source_id: UUID
    group: str | None = Field(default=None, min_length=1, max_length=80)


class CollectionWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=160)
    expected_revision: int = Field(ge=0)
    members: list[CollectionMember] = Field(max_length=500)

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
    offset: int = Field(default=0, ge=0, le=500)
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
    method: Literal["text-evidence-v1", "semantic-rerank-v1"] = "text-evidence-v1"
    moments: list[MediaMomentOut]
    unavailable_asset_ids: list[UUID]
    unindexed_asset_ids: list[UUID] = Field(default_factory=list)
    incomplete_speech_asset_ids: list[UUID] = Field(default_factory=list)
    truncated_asset_ids: list[UUID] = Field(default_factory=list)
    candidate_limit_reached: bool = False


class SemanticSearchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    query: str = Field(min_length=1, max_length=500)
    cache_only: bool = False


class StoryGenerationBody(SemanticSearchBody):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    research_topic: str | None = Field(default=None, min_length=4, max_length=200)
    allow_partial_analysis: bool = False

    @model_validator(mode="after")
    def public_research_topic(self):
        if self.research_topic is not None:
            from app.services.researched_text import ResearchRequest

            ResearchRequest(topic=self.research_topic, request_quote=self.research_topic)
        return self


class MomentInspectionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    moment_id: UUID
    question: str = Field(min_length=1, max_length=200)
    cache_only: bool = False


class MomentInspectionOut(BaseModel):
    moment_id: UUID
    source_id: UUID
    generation: str
    start_s: float
    end_s: float
    uncertain: Literal[True] = True
    verification: Literal["targeted_model_inspection"]
    answer: str
    confidence: float = Field(ge=0, le=1)
    evidence: str
    sample_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class StoryContentPlan(BaseModel):
    """Suggested creative wording, kept distinct from source-analysis descriptions."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    version: Literal[1] = 1
    format: Literal["text_story"] = "text_story"
    audience: str = Field(min_length=1, max_length=200)
    angle: str = Field(min_length=1, max_length=280)
    why_it_works: str = Field(min_length=1, max_length=500)
    opening_text: str = Field(min_length=1, max_length=100)
    closing_text: str | None = Field(default=None, min_length=1, max_length=100)
    caption: str = Field(default="", max_length=300)
    beat_reasons: list[str] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def bounded_reasons(self):
        if any(not reason.strip() or len(reason) > 240 for reason in self.beat_reasons):
            raise ValueError("Each beat needs a short reason")
        return self


class CollectionStoryOut(BaseModel):
    id: UUID
    collection_id: UUID
    collection_revision: int
    revision: int
    title: str
    hook: str
    payoff: str
    rationale: str
    content_plan: StoryContentPlan | None = None
    duration_s: float
    moments: list[MediaMomentOut]
    audio_policy: Literal["preserve_source_audio", "muted"] = "preserve_source_audio"
    on_screen_copy: list[str] = Field(default_factory=list)
    requires_render_approval: Literal[True] = True
    rejected: bool = False
    research: StoryResearch | None = None


class CollectionStoriesOut(BaseModel):
    revision: int
    stories: list[CollectionStoryOut]
    coverage: MomentSearchOut


class StoryReviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    expected_story_revision: int = Field(ge=1)
    client_event_id: str = Field(min_length=1, max_length=100)


class StoryCorrectionBody(StoryReviewBody):
    replace_index: int | None = Field(default=None, ge=0, le=7)
    moment_id: UUID | None = None
    audio_policy: Literal["preserve_source_audio", "muted"] | None = None
    moment_order: list[UUID] | None = Field(default=None, min_length=1, max_length=8)
    content_plan: StoryContentPlan | None = None

    @model_validator(mode="after")
    def one_scoped_change(self):
        replacement = self.replace_index is not None or self.moment_id is not None
        if replacement and (self.replace_index is None or self.moment_id is None):
            raise ValueError("Replacement requires both an index and a moment")
        if (
            sum(
                (
                    replacement,
                    self.audio_policy is not None,
                    self.moment_order is not None,
                    self.content_plan is not None,
                )
            )
            != 1
        ):
            raise ValueError("Change one story section at a time")
        if self.moment_order and len(set(self.moment_order)) != len(self.moment_order):
            raise ValueError("A story cannot repeat the same interval")
        return self


class StorySelectionBody(StoryReviewBody):
    thread_id: UUID
    expected_thread_revision: int = Field(ge=0)
