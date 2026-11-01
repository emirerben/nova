"""The Talk-to-camera green-screen lane: one picture or looping video behind the speaker.

The recipe only names the background. The phone cuts the speaker out of the speaker
clip with a person mask and composites it over this asset; nothing raster crosses the
boundary. Mirrored by `KriaMediaEngine.SpeakerBackgroundLayer` and pinned by
`tests/fixtures/speaker-background-parity/v1.json`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_SOURCE_START_S = 1800.0
DEFAULT_SUBJECT_TRACK_ID = "subtitled"


class SpeakerBackgroundLayer(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    asset_id: str = Field(min_length=1, max_length=160, pattern=r"^\S+$")
    media_kind: Literal["image", "video"]
    source_start: float = Field(default=0.0, ge=0, le=MAX_SOURCE_START_S)
    loop: bool = True
    subject_track_id: str = Field(default=DEFAULT_SUBJECT_TRACK_ID, min_length=1, max_length=160)

    @model_validator(mode="after")
    def photo_has_no_timeline(self) -> SpeakerBackgroundLayer:
        if self.media_kind == "image" and self.source_start != 0:
            raise ValueError("a photo background has no source start")
        return self
