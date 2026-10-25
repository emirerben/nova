"""Creator-confirmed taste and exact-proposal judgments; no media permissions."""

import json
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.services.private_text import normalize_private_text


class TasteWrite(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    expected_revision: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=128)
    project_ids: list[UUID] = Field(min_length=1, max_length=50)
    rules: list[str] = Field(default_factory=list, max_length=12)
    project_notes: dict[UUID, str] = Field(default_factory=dict, max_length=50)
    confirmed: Literal[True]
    enabled: bool = True

    @model_validator(mode="after")
    def bounded(self):
        if len(set(self.project_ids)) != len(self.project_ids):
            raise ValueError("Choose each project once")
        rules = [normalize_private_text(v).strip() for v in self.rules]
        if (
            any(not v or len(v) > 500 for v in rules)
            or sum(map(len, rules)) > 4000
            or len(json.dumps(rules, ensure_ascii=False).encode()) > 12000
        ):
            raise ValueError("Keep confirmed rules short")
        self.rules = rules
        if not self.project_notes.keys() <= set(self.project_ids):
            raise ValueError("Notes must refer to selected projects")
        self.project_notes = {
            key: normalize_private_text(value).strip() for key, value in self.project_notes.items()
        }
        if any(len(value) > 1500 for value in self.project_notes.values()):
            raise ValueError("Keep each project note short")
        return self


class TasteErase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=128)


class IdeaFeedbackWrite(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    collection_id: UUID
    story_id: UUID
    expected_story_revision: int = Field(ge=1)
    client_event_id: str = Field(min_length=1, max_length=128)
    verdict: Literal["would_post", "needs_work", "not_for_me"]
    reasons: list[
        Literal[
            "too_generic",
            "forced_question",
            "not_my_voice",
            "invented_experience",
            "footage_mismatch",
            "wrong_facts",
            "missing_point",
            "good_observation",
            "useful_detail",
            "good_mood",
            "other",
        ]
    ] = Field(default_factory=list, max_length=5)
    note: str = Field(default="", max_length=1000)
    rewrite: str = Field(default="", max_length=1000)
