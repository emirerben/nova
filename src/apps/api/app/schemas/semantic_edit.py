"""LLM-authored semantic edit intent, separate from its server schedule."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SemanticSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    media_id: str = Field(min_length=1, max_length=100)
    candidate_index: int | None = Field(default=None, ge=0)
    weight: float = Field(default=1.0, gt=0, allow_inf_nan=False)


class SemanticChapter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chapter_id: str = Field(min_length=1, max_length=100)
    topic: str = Field(min_length=1, max_length=80)
    thought: str = Field(default="", max_length=280)
    role: Literal["hook", "build", "payoff"]
    weight: float = Field(default=1.0, gt=0, allow_inf_nan=False)
    layout: Literal["fullscreen", "supporting_card"] = "fullscreen"
    # A single creator group can name every proposal source. The scheduler
    # later emits renderer-compatible consecutive groups of at most four.
    sources: list[SemanticSource] = Field(min_length=1, max_length=80)

    @model_validator(mode="after")
    def _source_ids_are_unique(self) -> SemanticChapter:
        if len({source.media_id for source in self.sources}) != len(self.sources):
            raise ValueError("a semantic chapter may name each source once")
        return self


class SemanticTextBinding(BaseModel):
    """Semantic placement targets; actual text is unchanged creator/agent copy."""

    text: str = Field(min_length=1, max_length=120)
    chapter_ids: list[str] = Field(default_factory=list, max_length=20)
    media_ids: list[str] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def _has_target(self) -> SemanticTextBinding:
        if not self.chapter_ids and not self.media_ids:
            raise ValueError("text binding needs a chapter or media target")
        return self


class SemanticEditPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    title: str = Field(default="", max_length=280)
    chapters: list[SemanticChapter] = Field(min_length=1, max_length=20)
    # Kept opaque here: the semantic agent imports both this contract and the
    # edit-proposal agent input, so importing MontageAudioPlan would make that
    # boundary circular.  The adapter supplies the already-validated object.
    montage_audio: Any | None = None
    text_bindings: list[SemanticTextBinding] = Field(default_factory=list, max_length=12)
    # Parse-time repairs are observable diagnostics. They never change the
    # server schedule or make model-owned timing authoritative.
    repairs: list[str] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def _chapter_ids_are_unique(self) -> SemanticEditPlan:
        if len({chapter.chapter_id for chapter in self.chapters}) != len(self.chapters):
            raise ValueError("semantic chapter IDs must be unique")
        return self
