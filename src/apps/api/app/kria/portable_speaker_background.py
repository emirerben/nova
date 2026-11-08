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

KRI-1173 (solid-colour fill): the FILL may instead be a flat colour -- ``media_kind:
"color"`` + ``color: "#RRGGBB"`` and NO ``asset_id`` -- pinned by
`tests/fixtures/speaker-background-parity/v3.json`. A media fill serialises exactly as
before (``color`` omitted while None), so every earlier recipe keeps its digest. Windows
stay photo/video only. Design and failure modes:
`docs/reviews/speaker-background-color-failure-modes.md`.

Qendresa item 16 (the speaker full screen in their own take, then the corner): optional
``full_screen_windows`` (absent while unused) are spans of the edit where the speaker's own
take is drawn full-frame and untouched -- no person mask, no background, no ``speaker_frame``.
Outside them the layer renders exactly as before. Capability ``speakerFrameWindows``; pinned by
`tests/fixtures/speaker-background-parity/v4.json`.

Qendresa item 18 (aim a screen recording at its readable text): optional ``focus`` on the FILL
(photo/video only) and on every window (absent while unused, never null in output) places the
picture like ``VisualMediaPlacement``: ``{x, y, zoom}`` where the base scale is cover, the drawn
scale is ``max(cover * zoom, contain)`` and ``x`` / ``y`` align the picture (0 = left / top edge,
0.5 = centre, 1 = right / bottom edge). Capability ``backgroundFocus``; pinned by
`tests/fixtures/speaker-background-parity/v5.json`.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

MAX_SOURCE_START_S = 1800.0
DEFAULT_SUBJECT_TRACK_ID = "subtitled"
# KRI-1162: the timed-background contract, enforced identically on the phone.
MAX_BACKGROUND_WINDOWS = 40
MIN_BACKGROUND_WINDOW_S = 0.5
MAX_TIMELINE_S = 1800.0
MIN_SPEAKER_FRAME_WIDTH_FRACTION = 0.2
# Qendresa item 16: an opening and (later) a closing full-frame span; a few, never a timeline.
MAX_FULL_SCREEN_WINDOWS = 4
# Qendresa item 18: wire bounds of a background focus zoom.
MIN_FOCUS_ZOOM = 0.2
MAX_FOCUS_ZOOM = 4.0
# KRI-1173: exactly ``#`` + six UPPERCASE hex digits on the recipe wire. Case and names are
# normalised at the editor Save, the lane and the chat boundary, never here.
BACKGROUND_COLOR_PATTERN = r"^#[0-9A-F]{6}$"

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


class SpeakerBackgroundFocus(BaseModel):
    """Qendresa item 18: where a background picture is aimed. ``x`` / ``y`` align the drawn
    picture in the frame (0 left/top, 0.5 centre, 1 right/bottom); ``zoom`` multiplies the cover
    scale (never drawn smaller than the whole picture)."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    zoom: float = Field(ge=MIN_FOCUS_ZOOM, le=MAX_FOCUS_ZOOM)


class SpeakerBackgroundWindow(BaseModel):
    """One timed background: ``asset_id`` shows from ``start`` to ``end`` (output seconds).
    A video plays from ``source_start`` and loops inside the window; a photo is held."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    asset_id: str = Field(min_length=1, max_length=160, pattern=r"^\S+$")
    media_kind: Literal["image", "video"]
    source_start: float = Field(default=0.0, ge=0, le=MAX_SOURCE_START_S)
    start: float = Field(ge=0.0, le=MAX_TIMELINE_S)
    end: float = Field(gt=0.0, le=MAX_TIMELINE_S)
    # Qendresa item 18, omitted from the wire while unused (never ``null``).
    focus: SpeakerBackgroundFocus | None = None

    @model_validator(mode="after")
    def _well_formed(self) -> SpeakerBackgroundWindow:
        if self.media_kind == "image" and self.source_start != 0:
            raise ValueError("a photo background has no source start")
        if self.end - self.start < MIN_BACKGROUND_WINDOW_S:
            raise ValueError("a background window must last at least 0.5 seconds")
        return self

    @model_serializer(mode="wrap")
    def _omit_unused(self, handler):  # noqa: ANN001, ANN202
        data = handler(self)
        if data.get("focus") is None:
            data.pop("focus", None)
        return data


class SpeakerFullScreenWindow(BaseModel):
    """Qendresa item 16: from ``start`` to ``end`` (output seconds) the speaker's own take shows
    full-frame and untouched (no mask, no background, no corner frame)."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    start: float = Field(ge=0.0, le=MAX_TIMELINE_S)
    end: float = Field(gt=0.0, le=MAX_TIMELINE_S)

    @model_validator(mode="after")
    def _long_enough(self) -> SpeakerFullScreenWindow:
        if self.end - self.start < MIN_BACKGROUND_WINDOW_S:
            raise ValueError("a full-screen window must last at least 0.5 seconds")
        return self


class SpeakerBackgroundLayer(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    # KRI-1173: absent (never null) for a colour fill; required for image | video.
    asset_id: str | None = Field(default=None, min_length=1, max_length=160, pattern=r"^\S+$")
    media_kind: Literal["image", "video", "color"]
    # KRI-1173: required iff ``media_kind == "color"``; omitted from the wire otherwise.
    color: str | None = Field(default=None, pattern=BACKGROUND_COLOR_PATTERN)
    source_start: float = Field(default=0.0, ge=0, le=MAX_SOURCE_START_S)
    loop: bool = True
    subject_track_id: str = Field(default=DEFAULT_SUBJECT_TRACK_ID, min_length=1, max_length=160)
    # KRI-1162, both omitted from the wire while unused (never ``[]`` / ``null``).
    windows: list[SpeakerBackgroundWindow] | None = Field(
        default=None, min_length=1, max_length=MAX_BACKGROUND_WINDOWS
    )
    speaker_frame: SpeakerFrame | None = None
    # Qendresa item 16, omitted from the wire while unused (never ``[]`` / ``null``).
    full_screen_windows: list[SpeakerFullScreenWindow] | None = Field(
        default=None, min_length=1, max_length=MAX_FULL_SCREEN_WINDOWS
    )
    # Qendresa item 18: aims the FILL picture (photo/video only); omitted while unused.
    focus: SpeakerBackgroundFocus | None = None

    @model_validator(mode="before")
    @classmethod
    def _colour_has_no_asset_key(cls, data: Any) -> Any:
        # A colour names no asset: even ``"asset_id": null`` is a malformed recipe (v3.json).
        if isinstance(data, dict) and data.get("media_kind") == "color" and "asset_id" in data:
            raise ValueError("a colour background has no asset_id")
        return data

    @model_validator(mode="after")
    def fill_matches_its_kind(self) -> SpeakerBackgroundLayer:
        if self.media_kind == "color":
            if self.color is None:
                raise ValueError("a colour background needs a color")
            if self.source_start != 0:
                raise ValueError("a colour background has no source start")
        else:
            if self.asset_id is None:
                raise ValueError("a photo or video background needs an asset_id")
            if self.color is not None:
                raise ValueError("only a colour background carries a color")
        if self.media_kind == "color" and self.focus is not None:
            raise ValueError("a colour background has no focus")
        return self

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

    @model_validator(mode="after")
    def full_screen_windows_sorted_and_disjoint(self) -> SpeakerBackgroundLayer:
        # The phone picks per frame whether the time is inside one, so they must be sorted and
        # never overlap (touching ends are fine), like the background windows (S28).
        if self.full_screen_windows:
            pairs = zip(self.full_screen_windows, self.full_screen_windows[1:], strict=False)
            for earlier, later in pairs:
                if later.start < earlier.end:
                    raise ValueError("full-screen windows must be sorted and must not overlap")
        return self

    @property
    def uses_windows(self) -> bool:
        """True when the layer needs the ``speakerBackgroundWindows`` capability."""
        return bool(self.windows) or self.speaker_frame is not None

    @property
    def uses_full_screen(self) -> bool:
        """True when the layer needs the ``speakerFrameWindows`` capability (Qendresa item 16)."""
        return bool(self.full_screen_windows)

    @property
    def uses_focus(self) -> bool:
        """True when the fill or any window is aimed (needs ``backgroundFocus``, item 18)."""
        return self.focus is not None or any(w.focus is not None for w in self.windows or ())

    @property
    def all_pictures_zoomed(self) -> bool:
        """KRI-1307: every picture drawn (a media fill and each window) is zoomed in (> 1x).

        Any focus at all is not enough: a recording drawn at 1x, or one window left with no focus,
        is not zoomed onto its text. A recipe with no picture at all is False.
        """
        pictures = [] if self.media_kind == "color" else [self.focus]
        pictures += [w.focus for w in self.windows or ()]
        return bool(pictures) and all(f is not None and f.zoom > 1.0 for f in pictures)

    @property
    def uses_color(self) -> bool:
        """True when the fill is a flat colour (needs the ``speakerBackgroundColor`` capability)."""
        return self.media_kind == "color"

    @model_serializer(mode="wrap")
    def _omit_unused(self, handler):  # noqa: ANN001, ANN202
        data = handler(self)
        if data.get("asset_id") is None:
            data.pop("asset_id", None)
        if data.get("color") is None:
            data.pop("color", None)
        if not data.get("windows"):
            data.pop("windows", None)
        if data.get("speaker_frame") is None:
            data.pop("speaker_frame", None)
        if not data.get("full_screen_windows"):
            data.pop("full_screen_windows", None)
        if data.get("focus") is None:
            data.pop("focus", None)
        return data
