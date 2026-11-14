"""Creator-authored panel source selection, separate from output placement."""

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.services.editor_limits import EDITOR_MIN_CLIP_S


class SplitPanelClip(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    media_id: str = Field(min_length=1, max_length=160)
    source_start_s: float | None = Field(default=None, ge=0, le=36000)
    duration_s: float | None = Field(default=None, ge=EDITOR_MIN_CLIP_S, le=1800)


class SplitPanelSource(BaseModel):
    """One panel: a single `media_id`, or `clips` that play one after another."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    media_id: str | None = Field(default=None, min_length=1, max_length=160)
    source_start_s: float | None = Field(default=None, ge=0, le=36000)
    clips: list[SplitPanelClip] | None = Field(default=None, min_length=1, max_length=8)

    @model_validator(mode="after")
    def one_source_or_a_clip_list(self):
        if (self.media_id is None) == (self.clips is None):
            raise ValueError("a panel needs either media_id or clips")
        if self.clips is not None and self.source_start_s is not None:
            raise ValueError("source_start_s belongs on each clip of a clip list")
        return self

    @property
    def sequence(self) -> list[SplitPanelClip]:
        if self.clips is not None:
            return self.clips
        return [SplitPanelClip(media_id=self.media_id, source_start_s=self.source_start_s)]


class InitialSplitScreen(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    panels: list[SplitPanelSource] = Field(min_length=2, max_length=3)
    start_s: float | None = Field(default=None, ge=0, le=1800)
    duration_s: float | None = Field(default=None, ge=EDITOR_MIN_CLIP_S, le=1800)
    panel_weights: list[float] | None = None
    gutter: float = Field(default=0, ge=0, le=0.1)
    background_color: str = Field(default="#000000", pattern=r"^#[0-9a-fA-F]{6}$")

    @model_validator(mode="after")
    def valid_layout(self):
        if self.panel_weights is not None and (
            len(self.panel_weights) != len(self.panels) or any(w <= 0 for w in self.panel_weights)
        ):
            raise ValueError("panel weights must be positive and match the panel sources")
        return self
