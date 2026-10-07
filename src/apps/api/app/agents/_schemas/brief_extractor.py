"""Typed contract for extracting only newly stated creative brief requirements."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.kria.brief import BriefUpdate


class BriefExtractionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    creator_request: str = Field(default="", max_length=12_000)
    user_message: str = Field(min_length=1, max_length=12_000)
    conversation: list[dict] = Field(default_factory=list, max_length=40)


class BriefExtractionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brief_updates: list[BriefUpdate] = Field(default_factory=list)
