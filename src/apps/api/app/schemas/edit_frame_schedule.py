"""Server-authored, frame-exact edit schedule.

This module deliberately has no dependency on the proposal schemas.  A
schedule is persisted with a proposal, but can also be validated by a compiler
without importing the semantic planning boundary.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class FrameScheduledMoment(BaseModel):
    moment_id: str = Field(min_length=1, max_length=100)
    beat_id: str = Field(min_length=1, max_length=100)
    media_id: str = Field(min_length=1, max_length=100)
    source_start_frame: int = Field(ge=0)
    source_end_frame: int = Field(gt=0)
    output_start_frame: int = Field(ge=0)
    output_end_frame: int = Field(gt=0)
    layout: Literal["fullscreen", "supporting_card"] = "fullscreen"
    role: Literal["hook", "build", "payoff"]

    @model_validator(mode="after")
    def _valid_ranges(self) -> FrameScheduledMoment:
        if self.source_end_frame <= self.source_start_frame:
            raise ValueError("source frame range must be positive")
        if self.output_end_frame <= self.output_start_frame:
            raise ValueError("output frame range must be positive")
        if (self.source_end_frame - self.source_start_frame) != (
            self.output_end_frame - self.output_start_frame
        ):
            raise ValueError("source and output frame ranges must have equal duration")
        return self


class EditFrameSchedule(BaseModel):
    schema_version: Literal[1] = 1
    fps: Literal[30] = 30
    total_frames: int = Field(gt=0)
    transition_frames: int = Field(ge=0)
    direction: Literal["guided_story", "fast_montage", "text_explainer"]
    moments: list[FrameScheduledMoment] = Field(min_length=1, max_length=80)

    @model_validator(mode="after")
    def _timeline_is_canonical(self) -> EditFrameSchedule:
        if self.moments[0].output_start_frame != 0:
            raise ValueError("schedule must start at output frame zero")
        if self.moments[-1].output_end_frame != self.total_frames:
            raise ValueError("schedule must end at total_frames")
        if len(self.moments) > 1 and any(
            moment.output_end_frame - moment.output_start_frame <= self.transition_frames
            for moment in self.moments
        ):
            raise ValueError("each moment must advance beyond its transition overlap")
        previous = self.moments[0]
        for moment in self.moments[1:]:
            overlap = previous.output_end_frame - moment.output_start_frame
            if overlap != self.transition_frames:
                raise ValueError("schedule moments must use the canonical transition overlap")
            previous = moment
        return self
