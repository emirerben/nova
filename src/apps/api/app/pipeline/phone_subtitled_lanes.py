"""Lane models for the phone "Talking to camera" (subtitled) recipe compiler
(KRI-174 Phase 1).

The subtitled phone recipe is one speaker clip + Whisper captions today
(`app.pipeline.phone_subtitled_plan`). These models let it ALSO carry:

  - ``overlays``: sticker/photo cards drawn from the creator's Visuals pool,
    composited as a silent overlay track (mirrors
    `app.pipeline.phone_editor_visuals.compile_editor_media_track`'s clip
    shape). A card can also bind a VIDEO Visual (`kind="video"`, KRI-183):
    it plays muted, starting at ``source_start_s``, and its on-screen window
    shortens to match the footage when the source runs out before the
    card's spoken window does.
  - ``sound_effects``: one shared audio track of catalog sound effects.
  - ``ending_clip``: an optional MUTED video clip from the Visuals pool
    appended after the speaker clip on the same main video track.

``PhoneSubtitledLaneRequest`` is the server-authored, unresolved shape
persisted on ``Job.assembly_plan[PHONE_SUBTITLED_LANES_FIELD]``.
``PhoneSubtitledLanes`` is what the compiler actually consumes: sound
effects have already been resolved against the catalog into
``ResolvedSoundEffect`` (asset identity + probed duration), the same
resolve-then-compile split every other phone lane follows.

Every lane fails closed: a lane that cannot compile raises
``SubtitledLaneError`` (a ``UnsupportedPhonePlan`` subclass carrying
``.lane``) instead of raising a bare exception or silently dropping content
that was supposed to render. The runner is expected to catch
``SubtitledLaneError`` per attempt and retry the compile with that one lane
dropped via ``drop_lane`` -- see `lane_names`/`drop_lane` below.
"""

from __future__ import annotations

from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

from app.kria.render_assets import LibraryRenderAsset
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan

# `Job.assembly_plan` key the runner reads/writes `PhoneSubtitledLaneRequest`
# from. Versioned so a future incompatible shape can land beside it.
PHONE_SUBTITLED_LANES_FIELD = "_phone_subtitled_lanes_v1"

# The device audio engine only plays these container/codec combinations back
# directly -- a sound effect catalog row whose `audio_gcs_path` doesn't end in
# one of these is not phone-playable regardless of catalog status.
PLAYABLE_SFX_EXTENSIONS = frozenset({".m4a", ".wav", ".mp3", ".aac"})

# Captions render with their band anchored around y~=0.8 of the canvas
# (`app.pipeline.phone_captions`). Overlay cards must stay clear of that band,
# so a card's own y-fraction is clamped to no lower than this fraction of the
# canvas height.
CAPTION_BAND_TOP_FRAC = 0.62

_ID_PATTERN = r"^\S+$"


class _LaneModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class SubtitledOverlayCard(_LaneModel):
    """One sticker/photo/video card requested over the speaker clip."""

    id: str = Field(min_length=1, max_length=80, pattern=_ID_PATTERN)
    media_id: str = Field(min_length=1, max_length=160, pattern=_ID_PATTERN)
    # Server-derived pin (not creator input): the exact bytes the runner
    # already resolved via `bind_phone_visual_assets`, passed straight
    # through to `require_bound_visual`.
    gcs_path: str = Field(min_length=1)
    generation: str = Field(min_length=1, max_length=160, pattern=_ID_PATTERN)
    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)
    x_frac: float = Field(default=0.5, ge=0, le=1)
    y_frac: float = Field(default=0.4, ge=0, le=1)
    # Width fraction of the canvas the card occupies.
    scale: float = Field(default=0.35, ge=0.05, le=1)
    fade: bool = False
    z: int = Field(default=0, ge=0)
    # KRI-183: a card may bind a VIDEO Visual instead of a photo. "video"
    # plays muted starting at `source_start_s`; the compiler shortens the
    # card's on-screen window to match the footage when the source runs out
    # before the spoken window does (the device never holds the last frame).
    kind: Literal["image", "video"] = "image"
    source_start_s: float = Field(default=0.0, ge=0)

    @model_validator(mode="after")
    def _window(self) -> SubtitledOverlayCard:
        if self.end_s <= self.start_s:
            raise ValueError("overlay card must have a positive-duration window")
        return self

    @model_serializer(mode="wrap")
    def _image_shape(self, handler: SerializerFunctionWrapHandler) -> dict:
        # A photo card keeps the pre-KRI-183 persisted shape (mirrors
        # `PhoneVisualBinding._photo_shape` in app/services/phone_sources.py):
        # `kind`/`source_start_s` only appear once a card actually opts into
        # video, so a photo card's dump/DB row is byte-identical to before.
        payload = handler(self)
        if self.kind == "image":
            payload.pop("kind", None)
            payload.pop("source_start_s", None)
        return payload


class SubtitledSoundEffect(_LaneModel):
    """One catalog sound effect requested at a point on the timeline."""

    id: str = Field(min_length=1, max_length=80, pattern=_ID_PATTERN)
    catalog_id: str = Field(min_length=1, max_length=160, pattern=_ID_PATTERN)
    at_s: float = Field(ge=0)
    volume: float = Field(default=1.0, ge=0, le=2)


class SubtitledEndingClip(_LaneModel):
    """An optional muted Visuals-pool video appended after the speaker clip."""

    media_id: str = Field(min_length=1, max_length=160, pattern=_ID_PATTERN)
    gcs_path: str = Field(min_length=1)
    generation: str = Field(min_length=1, max_length=160, pattern=_ID_PATTERN)
    trim_start_s: float = Field(default=0.0, ge=0)
    max_duration_s: float | None = Field(default=None, gt=0)


class PhoneSubtitledLaneRequest(_LaneModel):
    """The unresolved lane request persisted on
    ``assembly_plan[PHONE_SUBTITLED_LANES_FIELD]``."""

    overlays: list[SubtitledOverlayCard] = Field(default_factory=list)
    sound_effects: list[SubtitledSoundEffect] = Field(default_factory=list)
    ending_clip: SubtitledEndingClip | None = None


class ResolvedSoundEffect(_LaneModel):
    """A `SubtitledSoundEffect` request resolved against the catalog: its
    render-manifest identity and its probed playable duration."""

    request: SubtitledSoundEffect
    asset: LibraryRenderAsset
    duration_s: float = Field(gt=0)

    @model_validator(mode="after")
    def _catalog(self) -> ResolvedSoundEffect:
        if self.asset.catalog != "sound_effect":
            raise ValueError("resolved sound effect must reference the sound_effect catalog")
        return self


class PhoneSubtitledLanes(_LaneModel):
    """What `compile_phone_subtitled_plan` actually consumes: sound effects
    are already resolved (`ResolvedSoundEffect`), unlike the raw
    `PhoneSubtitledLaneRequest` the runner persists."""

    overlays: list[SubtitledOverlayCard] = Field(default_factory=list)
    sound_effects: list[ResolvedSoundEffect] = Field(default_factory=list)
    ending_clip: SubtitledEndingClip | None = None


class SubtitledLaneError(UnsupportedPhonePlan):
    """One subtitled lane failed to compile.

    ``lane`` is one of ``"overlays"``, ``"sound_effects"``, ``"ending_clip"``
    -- exactly the names `lane_names`/`drop_lane` use -- so the runner can
    identify and drop the failing lane without parsing the message text.
    """

    def __init__(self, lane: str, message: str, *, capability: str | None = None) -> None:
        super().__init__(message, capability=capability)
        self.lane = lane


def _lane_error(lane: str, message: str, *, capability: str | None = None) -> SubtitledLaneError:
    return SubtitledLaneError(lane, f"subtitled {lane} lane: {message}", capability=capability)


def sfx_path_is_playable(path: str) -> bool:
    """True when ``path``'s extension is one the device audio engine can
    play back directly (case-insensitive)."""
    lowered = path.lower()
    return any(lowered.endswith(extension) for extension in PLAYABLE_SFX_EXTENSIONS)


def lane_names(lanes: PhoneSubtitledLanes | None) -> tuple[str, ...]:
    """Which of ``("overlays", "sound_effects", "ending_clip")`` carry
    content, in that fixed order. Empty/``None`` lanes yield ``()``."""
    if lanes is None:
        return ()
    names: list[str] = []
    if lanes.overlays:
        names.append("overlays")
    if lanes.sound_effects:
        names.append("sound_effects")
    if lanes.ending_clip is not None:
        names.append("ending_clip")
    return tuple(names)


def drop_lane(lanes: PhoneSubtitledLanes, name: str) -> PhoneSubtitledLanes:
    """A copy of ``lanes`` with lane ``name`` emptied, so the runner can
    retry a compile without the lane that just failed."""
    if name == "overlays":
        return lanes.model_copy(update={"overlays": []})
    if name == "sound_effects":
        return lanes.model_copy(update={"sound_effects": []})
    if name == "ending_clip":
        return lanes.model_copy(update={"ending_clip": None})
    raise ValueError(f"unknown subtitled lane: {name}")
