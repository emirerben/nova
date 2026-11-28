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
    # Parent KRI-1151: the item can pause the creator's take for one of its clips right now
    # (`insert_clip_candidates`), so the prompt may record that ask. Closed (default): the prompt
    # is byte-identical to before inserts existed.
    insert_clips_open: bool = False
    # KRI-1360: this message answers the creator's "Change my request" tap. False (default):
    # the prompt is byte-identical and the field is left out of input dumps.
    changes_request: bool = Field(default=False, exclude_if=lambda v: not v)
    # The chat's stored language, as context for `conversation_language` on a live rendered
    # follow-up (`require_request_scope`). None: left out of input dumps, prompt unchanged.
    reply_language: str | None = Field(default=None, max_length=8, exclude_if=lambda v: v is None)


class BriefExtractionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brief_updates: list[BriefUpdate] = Field(default_factory=list)
    # KRI-814: typed "the current message states no requirement and delegates every choice".
    delegates_all_choices: bool = False
    # KRI-816: typed "start fresh": the server withdraws every saved requirement.
    starts_fresh: bool = False
    request_scope: Literal["edit", "rebuild", "preview", "clarify"] | None = None
    clarification: str | None = Field(default=None, max_length=1_000)
    # The language the creator writes to Kria in, read from their own words (never the video's
    # text). None = could not tell or not offered now: the chat keeps its stored language.
    conversation_language: Literal["en", "tr", "de"] | None = Field(
        default=None, exclude_if=lambda v: v is None
    )
