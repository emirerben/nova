"""Creator-authored panel source selection, separate from output placement."""

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SplitPanelSource(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    media_id: str = Field(min_length=1, max_length=160)
    source_start_s: float | None = Field(default=None, ge=0, le=36000)


class InitialSplitScreen(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    panels: list[SplitPanelSource] = Field(min_length=2, max_length=3)
    start_s: float | None = Field(default=None, ge=0, le=1800)
    duration_s: float | None = Field(default=None, ge=0.1, le=1800)
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
