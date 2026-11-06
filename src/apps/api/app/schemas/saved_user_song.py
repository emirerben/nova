"""Saved editor choices, distinct from the last compiled song plan."""

import uuid

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.guided_edit_revision import GuidedEditorUserSong


class SavedUserSong(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: uuid.UUID
    variant_id: str = Field(min_length=1)
    generation: str = Field(min_length=1)
    revision_number: int = Field(ge=1)
    state: GuidedEditorUserSong
