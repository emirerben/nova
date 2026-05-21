from pydantic import BaseModel, ConfigDict, Field


class LyricsConfigOverride(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pre_roll_s: float | None = Field(default=None, ge=0.0, le=2.0)
    post_dwell_s: float | None = Field(default=None, ge=0.0, le=5.0)
    next_line_gap_s: float | None = Field(default=None, ge=0.0, le=2.0)
    fade_in_ms: int | None = Field(default=None, ge=0, le=2000)
    fade_out_ms: int | None = Field(default=None, ge=0, le=2000)
    hold_to_next_threshold_ms: int | None = Field(default=None, ge=0, le=5000)
    font_family: str | None = None
