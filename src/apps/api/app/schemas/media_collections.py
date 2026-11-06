"""Bounded, owned catalogue selections. Never accepts a Photos identifier or bytes."""

from datetime import datetime
from typing import Annotated, Literal, NamedTuple
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.schemas.research import StoryResearch

# A structural bound on how many moments one idea may cut together, not an editorial one: the
# 60-second total and what the story needs decide the count, so this only stops a runaway list.
MAX_STORY_MOMENTS = 60


class CollectionMember(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_id: UUID
    source_id: UUID
    group: str | None = Field(default=None, min_length=1, max_length=80)


class CollectionEvent(BaseModel):
    """Creator-written context for one event, keyed by the members' `group` string.

    `group` need not match any member: a brand-new event is empty until footage is added.
    """

    model_config = ConfigDict(extra="forbid")
    group: str = Field(min_length=1, max_length=80)
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    about: Annotated[str, StringConstraints(strip_whitespace=True, max_length=600)] = ""


class CollectionWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=160)
    expected_revision: int = Field(ge=0)
    members: list[CollectionMember] = Field(max_length=500)
    # None (omitted) keeps the stored events; a list, even empty, replaces them. Builds that
    # predate events still PUT whole selections and must not erase what another device wrote.
    events: list[CollectionEvent] | None = Field(default=None, max_length=50)

    @model_validator(mode="after")
    def unique_members(self):
        if len({member.asset_id for member in self.members}) != len(self.members):
            raise ValueError("Each catalogue asset may appear only once")
        if not self.name.strip():
            raise ValueError("Name must not be blank")
        if self.events and len({value.group for value in self.events}) != len(self.events):
            raise ValueError("Each event may be described only once")
        return self


class CollectionOut(BaseModel):
    id: UUID
    name: str
    revision: int
    members: list[CollectionMember]
    unavailable_asset_ids: list[UUID] = Field(default_factory=list)
    events: list[CollectionEvent] = Field(default_factory=list)


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
    context_asset_ids: list[UUID] = Field(default_factory=list)
    # Long clips with no detected moment, offered as a fixed sampled window.
    sampled_asset_ids: list[UUID] = Field(default_factory=list)
    unusable_timing_asset_ids: list[UUID] = Field(default_factory=list)


ItemState = Literal[
    "queued", "uploading", "analyzing", "ready", "needs_retry", "failed", "excluded"
]
ItemReason = Literal[
    "provider_error",
    "budget_paused",
    "unsupported_type",
    "too_large",
    "too_long",
    "icloud_unavailable",
    "no_usable_moment",
    "access_revoked",
]


class CollectionItemOut(BaseModel):
    """One collection member's server-derived progress. See media_collection_items.py.

    `uploading` and `icloud_unavailable` are device-side: a member's bytes are on the server
    by definition, so the client owns both while it exports and uploads.
    """

    asset_id: UUID
    kind: Literal["video", "image"]
    group: str | None = None
    state: ItemState
    reason: ItemReason | None = None
    retryable: bool = False
    attempts: int = 0
    updated_at: datetime


class CollectionItemsOut(BaseModel):
    revision: int
    items: list[CollectionItemOut]


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
    evidence_scope: Literal["timed_moment", "whole_source_context"] = "timed_moment"
    evidence: dict


class MomentSearchOut(BaseModel):
    revision: int
    method: Literal["text-evidence-v1", "semantic-rerank-v1"] = "text-evidence-v1"
    moments: list[MediaMomentOut]
    unavailable_asset_ids: list[UUID]
    unindexed_asset_ids: list[UUID] = Field(default_factory=list)
    incomplete_speech_asset_ids: list[UUID] = Field(default_factory=list)
    # Sources whose own interval list was cut short by the per-source cap.
    truncated_asset_ids: list[UUID] = Field(default_factory=list)
    # Indexed sources with no offered candidate because the candidate cap was reached;
    # `candidate_limit_reached` is true exactly when that cap was hit.
    unoffered_asset_ids: list[UUID] = Field(default_factory=list)
    candidate_limit_reached: bool = False
    # Videos bound to a source that cannot run on the phone (no analysis proxy).
    unsupported_asset_ids: list[UUID] = Field(default_factory=list)


class SemanticSearchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    query: str = Field(min_length=1, max_length=500)
    cache_only: bool = False


class StoryGenerationBody(SemanticSearchBody):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    research_topic: str | None = Field(default=None, min_length=4, max_length=200)
    allow_partial_analysis: bool = False
    # Ideas are per event: evidence is limited to the members whose group equals this.
    # Absent keeps the whole-collection behaviour of older builds.
    group: str | None = Field(default=None, min_length=1, max_length=80)
    # An event's creator-written title and notes can stand in for a typed request.
    query: str = Field(default="", max_length=500)
    # The event's existing ideas. Non-empty means "one more idea": it is added beside them,
    # never replaces them, and never repeats their footage or wording.
    exclude_story_ids: list[UUID] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def public_research_topic(self):
        if not self.query and self.group is None:
            raise ValueError("A request for the whole collection needs a query")
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


StoryForm = Literal["guide", "factual_story", "atmosphere", "review", "performance"]


class FormEvidence(NamedTuple):
    research_facts: int = 0
    source_stops: int = 0


# What the offered footage/research must contain before an idea may claim a form.
FORM_EVIDENCE: dict[StoryForm, FormEvidence] = {
    "guide": FormEvidence(source_stops=3),
    "factual_story": FormEvidence(research_facts=1),
    "atmosphere": FormEvidence(),
    "review": FormEvidence(),
    "performance": FormEvidence(),
}


class StoryOpenQuestion(BaseModel):
    """An unknown the creator must answer or skip before the idea is built."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(default="", max_length=8)
    question: str = Field(min_length=1, max_length=140)
    answer: str | None = Field(default=None, min_length=1, max_length=200)
    dismissed: bool = False

    @model_validator(mode="after")
    def answered_or_skipped(self):
        if self.answer is not None and self.dismissed:
            raise ValueError("A question is either answered or skipped")
        return self


class StoryContentPlan(BaseModel):
    """Suggested creative wording, kept distinct from source-analysis descriptions."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    version: Literal[1] = 1
    format: Literal["text_story"] = "text_story"
    form: StoryForm | None = Field(default=None, exclude_if=lambda value: value is None)
    audience: str = Field(min_length=1, max_length=200)
    angle: str = Field(min_length=1, max_length=280)
    why_it_works: str = Field(min_length=1, max_length=500)
    opening_text: str = Field(min_length=1, max_length=100)
    closing_text: str | None = Field(default=None, min_length=1, max_length=100)
    caption: Annotated[str, StringConstraints(strip_whitespace=False)] = Field(
        default="", max_length=300
    )
    beat_reasons: list[str] = Field(min_length=1, max_length=MAX_STORY_MOMENTS)
    open_questions: list[StoryOpenQuestion] = Field(
        default_factory=list, max_length=3, exclude_if=lambda value: not value
    )

    @model_validator(mode="after")
    def bounded_reasons(self):
        if any(not reason.strip() or len(reason) > 240 for reason in self.beat_reasons):
            raise ValueError("Each beat needs a short reason")
        for position, question in enumerate(self.open_questions, 1):
            question.id = question.id or f"q{position}"
        if len({question.id for question in self.open_questions}) != len(self.open_questions):
            raise ValueError("Open questions need distinct ids")
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
    group: str | None = None


class CollectionStoriesOut(BaseModel):
    revision: int
    stories: list[CollectionStoryOut]
    coverage: MomentSearchOut
    suggested_research_topic: str | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class SavedCollectionStoriesOut(BaseModel):
    revision: int
    stories: list[CollectionStoryOut] = Field(max_length=20)


class StoryDeletedOut(BaseModel):
    """An idea is soft-deleted; restoring it is possible until this moment."""

    restore_until: datetime


class GroupDeleteBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    group: str = Field(min_length=1, max_length=80)
    expected_revision: int = Field(ge=1)


class GroupDeleteOut(BaseModel):
    revision: int
    deleted_story_ids: list[UUID]


class StoryReviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    expected_story_revision: int = Field(ge=1)
    client_event_id: str = Field(min_length=1, max_length=100)


class StoryCorrectionBody(StoryReviewBody):
    replace_index: int | None = Field(default=None, ge=0, le=MAX_STORY_MOMENTS - 1)
    moment_id: UUID | None = None
    audio_policy: Literal["preserve_source_audio", "muted"] | None = None
    moment_order: list[UUID] | None = Field(
        default=None, min_length=1, max_length=MAX_STORY_MOMENTS
    )
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
    recover_held_turn_id: UUID | None = None
