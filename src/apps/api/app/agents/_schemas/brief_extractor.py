"""Typed contract for extracting only newly stated creative brief requirements."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.kria.brief import BriefUpdate, CreativeBrief


class BriefExtractionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    creator_request: str = Field(default="", max_length=12_000)
    user_message: str = Field(min_length=1, max_length=12_000)
    conversation: list[dict] = Field(default_factory=list, max_length=40)
    # Internal ledger context.  When supplied by the planner, contextual
    # change/remove batches are checked before the model run is accepted so a
    # schema retry can repair a stale target or version.  Omitted for legacy
    # callers that only exercise additive extraction.
    current_brief: CreativeBrief | None = None
    # Live rendered follow-ups need the model's semantic routing decision before
    # the deterministic router considers legacy wording.  Direct/offline callers
    # retain the old brief-only contract by leaving this false.
    require_request_scope: bool = False


class BriefExtractionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brief_updates: list[BriefUpdate] = Field(default_factory=list)
    request_scope: Literal["edit", "rebuild", "clarify"] | None = None
    clarification: str | None = Field(default=None, max_length=1_000)
