"""The proposed story, with every beat bound to owned footage before approval."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

MediaId = Annotated[str, Field(min_length=1, max_length=100)]


class EditorialBeat(BaseModel):
    model_config = ConfigDict(extra="forbid")
    purpose: str = Field(min_length=1, max_length=80)
    media_ids: list[MediaId] = Field(min_length=1, max_length=50)
    # A writing task, not exact copy or a purported historical fact.
    text_goal: str | None = Field(default=None, max_length=240)


class EditorialPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    viewer_goal: str = Field(min_length=1, max_length=240)
    beats: list[EditorialBeat] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def unique_sources(self):
        ids = [media_id for beat in self.beats for media_id in beat.media_ids]
        if len(ids) > 50 or len(ids) != len(set(ids)):
            raise ValueError("editorial plan must place each source exactly once")
        return self
