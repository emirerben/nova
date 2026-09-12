"""Bounded source-camera program evaluated on the output clock."""

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CameraPulse(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    id: str = Field(min_length=1, max_length=160)
    start: float = Field(ge=0, le=1800)
    end: float = Field(gt=0, le=1800)
    intensity: float = Field(ge=0, le=0.08)

    @model_validator(mode="after")
    def valid_window(self):
        if self.end <= self.start:
            raise ValueError("camera pulse must have positive duration")
        return self
