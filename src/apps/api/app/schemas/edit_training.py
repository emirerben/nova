"""Provider-neutral contracts for edit-feedback dataset preparation."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

DatasetSplit = Literal["train", "validation", "test"]
EditAgent = Literal["edit_guide", "edit_proposal", "edit_copilot"]


class CanonicalEditTrainingRecord(BaseModel):
    """Consent-safe record shared by JSONL and Parquet encoders."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    artifact_key: str = Field(min_length=64, max_length=64)
    creator_group: str = Field(min_length=64, max_length=64)
    plan_item_group: str = Field(min_length=64, max_length=64)
    split: DatasetSplit
    agent: EditAgent
    media_summary: list[dict[str, Any]]
    user_intent: str
    proposed: dict[str, Any]
    execution: dict[str, Any]
    labels: list[dict[str, Any]]
    versions: dict[str, str]
    lineage_key: str = Field(min_length=64, max_length=64)


class EditPreferencePair(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    agent: EditAgent
    split: DatasetSplit
    lineage_key: str
    context: dict[str, Any]
    chosen: dict[str, Any]
    rejected: dict[str, Any]
    rationale: str


class DatasetReadiness(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ready: bool
    reviewed_artifacts: int = Field(ge=0)
    creator_groups: int = Field(ge=0)
    missing_dimensions: dict[str, int] = Field(default_factory=dict)
    blockers: list[str] = Field(default_factory=list)
