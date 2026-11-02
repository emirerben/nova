"""The Talk-to-camera green-screen lane: a picture or looping video behind the speaker.

The recipe only names the background. The phone cuts the speaker out of the speaker
clip with a person mask and composites it over this asset; nothing raster crosses the
boundary. Mirrored by `KriaMediaEngine.SpeakerBackgroundLayer` and pinned by
`tests/fixtures/speaker-background-parity/v1.json` (the first cut) and `v2.json`
(KRI-1162: timed ``windows`` and a ``speaker_frame``).

KRI-1162 (commentary-style green screen): the top-level fields stay the FILL background
for the whole edit; ``windows`` (absent while unused) override it in their time span, each
with its own pool photo or muted looping video; ``speaker_frame`` (absent while unused)
draws the matted speaker scaled into a corner instead of full-frame. A layer with neither
new field serialises byte-for-byte as before, so every first-cut recipe keeps its digest.
Design and failure modes: `docs/reviews/speaker-background-windows-failure-modes.md`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

MAX_SOURCE_START_S = 1800.0
DEFAULT_SUBJECT_TRACK_ID = "subtitled"
# KRI-1162: the timed-background contract, enforced identically on the phone.
MAX_BACKGROUND_WINDOWS = 40
MIN_BACKGROUND_WINDOW_S = 0.5
MAX_TIMELINE_S = 1800.0
MIN_SPEAKER_FRAME_WIDTH_FRACTION = 0.2

SpeakerFramePreset = Literal[
    "full", "small_bottom_right", "small_bottom_left", "small_top_right", "small_top_left"
]
# The only geometry the chat and the editor ever write (S37): a named corner, compiled here.
# ``full`` is the absence of a frame. Fractions are the CENTRE of the scaled speaker frame.
SPEAKER_FRAME_PRESETS: dict[str, dict[str, float]] = {
    "small_bottom_right": {"width_fraction": 0.42, "x_fraction": 0.76, "y_fraction": 0.80},
    "small_bottom_left": {"width_fraction": 0.42, "x_fraction": 0.24, "y_fraction": 0.80},
    "small_top_right": {"width_fraction": 0.42, "x_fraction": 0.76, "y_fraction": 0.22},
    "small_top_left": {"width_fraction": 0.42, "x_fraction": 0.24, "y_fraction": 0.22},
}


class SpeakerFrame(BaseModel):
    """Where the matted speaker is drawn: uniformly scaled so its width is
    ``width_fraction`` of the canvas width, centred at (``x_fraction``, ``y_fraction``).
    The person mask is computed on the full-size speaker layer first (S27)."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    width_fraction: float = Field(ge=MIN_SPEAKER_FRAME_WIDTH_FRACTION, le=1.0)
    x_fraction: float = Field(ge=0.0, le=1.0)
    y_fraction: float = Field(ge=0.0, le=1.0)


def speaker_frame_for_preset(preset: str | None) -> SpeakerFrame | None:
    """The frame a preset names; ``None`` for ``full``, ``None`` or an unknown name."""
    values = SPEAKER_FRAME_PRESETS.get(preset or "")
    return SpeakerFrame(**values) if values else None


class SpeakerBackgroundWindow(BaseModel):
    """One timed background: ``asset_id`` shows from ``start`` to ``end`` (output seconds).
    A video plays from ``source_start`` and loops inside the window; a photo is held."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    asset_id: str = Field(min_length=1, max_length=160, pattern=r"^\S+$")
    media_kind: Literal["image", "video"]
    source_start: float = Field(default=0.0, ge=0, le=MAX_SOURCE_START_S)
    start: float = Field(ge=0.0, le=MAX_TIMELINE_S)
    end: float = Field(gt=0.0, le=MAX_TIMELINE_S)

    @model_validator(mode="after")
    def _well_formed(self) -> SpeakerBackgroundWindow:
        if self.media_kind == "image" and self.source_start != 0:
            raise ValueError("a photo background has no source start")
        if self.end - self.start < MIN_BACKGROUND_WINDOW_S:
            raise ValueError("a background window must last at least 0.5 seconds")
        return self


class SpeakerBackgroundLayer(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    asset_id: str = Field(min_length=1, max_length=160, pattern=r"^\S+$")
    media_kind: Literal["image", "video"]
    source_start: float = Field(default=0.0, ge=0, le=MAX_SOURCE_START_S)
    loop: bool = True
    subject_track_id: str = Field(default=DEFAULT_SUBJECT_TRACK_ID, min_length=1, max_length=160)
    # KRI-1162, both omitted from the wire while unused (never ``[]`` / ``null``).
    windows: list[SpeakerBackgroundWindow] | None = Field(
        default=None, min_length=1, max_length=MAX_BACKGROUND_WINDOWS
    )
    speaker_frame: SpeakerFrame | None = None

    @model_validator(mode="after")
    def photo_has_no_timeline(self) -> SpeakerBackgroundLayer:
        if self.media_kind == "image" and self.source_start != 0:
            raise ValueError("a photo background has no source start")
        return self

    @model_validator(mode="after")
    def windows_sorted_and_disjoint(self) -> SpeakerBackgroundLayer:
        # S28: the compositor picks THE window containing the time, so windows must be
        # sorted by start and never overlap (touching ends are fine).
        if self.windows:
            for earlier, later in zip(self.windows, self.windows[1:], strict=False):
                if later.start < earlier.end:
                    raise ValueError("background windows must be sorted and must not overlap")
        return self

    @property
    def uses_windows(self) -> bool:
        """True when the layer needs the ``speakerBackgroundWindows`` capability."""
        return bool(self.windows) or self.speaker_frame is not None

    @model_serializer(mode="wrap")
    def _omit_unused(self, handler):  # noqa: ANN001, ANN202
        data = handler(self)
        if not data.get("windows"):
            data.pop("windows", None)
        if data.get("speaker_frame") is None:
            data.pop("speaker_frame", None)
        return data
