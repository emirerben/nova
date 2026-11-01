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
  - ``sound_effects``: one shared audio track of catalog sound effects,
    optionally trimmed to a sub-range of the catalog effect's own source
    audio (KRI-182 step 1).
  - ``ending_clip``: an optional video clip from the Visuals pool appended
    after the speaker clip on the same main video track. Muted unless the
    creator asked for its own sound (``volume``, KRI-841).
  - ``background``: one Visuals-pool photo or looping video the phone shows
    behind the speaker's person mask for the whole edit (KRI-1117, the
    Talk-to-camera "green screen"). Always muted, never retimed by speech cuts.

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

from typing import Literal, TypeVar

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
from app.pipeline.silence_cut import CutPlan  # noqa: TCH001

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

# Mirrors `kria.portable_speaker_background.MAX_SOURCE_START_S`.
MAX_BACKGROUND_SOURCE_START_S = 1800.0


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
    # KRI-297: "fullscreen" is a full-canvas cover-fill cutaway over the
    # speaker (muted; speaker audio and captions are untouched). x_frac /
    # y_frac / scale / z are ignored for it -- there is no caption-band clamp
    # because the card covers the frame. Omitted from the dump while "pip".
    display_mode: Literal["pip", "fullscreen"] = "pip"
    # KRI-1141: the card's tilt, degrees CLOCKWISE on screen, -180..180, 0 upright (the same
    # sense as `ClipTilt.rotation_deg` and the recipe's `transform.rotation_degrees`, which is
    # where the compile writes it). A pip card only: a full-canvas cutaway has nothing to turn
    # about. Omitted from the dump while 0, so an untilted card's row is byte-identical.
    rotation_deg: float = Field(default=0.0, ge=-180, le=180)

    @model_validator(mode="after")
    def _window(self) -> SubtitledOverlayCard:
        if self.end_s <= self.start_s:
            raise ValueError("overlay card must have a positive-duration window")
        if self.rotation_deg != 0 and self.display_mode == "fullscreen":
            raise ValueError("a full-screen overlay card cannot be tilted")
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
        if self.display_mode == "pip":
            payload.pop("display_mode", None)
        if self.rotation_deg == 0:
            payload.pop("rotation_deg", None)
        return payload


class SubtitledSoundEffect(_LaneModel):
    """One catalog sound effect requested at a point on the timeline."""

    id: str = Field(min_length=1, max_length=80, pattern=_ID_PATTERN)
    catalog_id: str = Field(min_length=1, max_length=160, pattern=_ID_PATTERN)
    at_s: float = Field(ge=0)
    volume: float = Field(default=1.0, ge=0, le=2)
    # Optional trim bounds within the catalog effect's own resolved source
    # audio (KRI-182 step 1 -- the native editor already edits these on a
    # generic `SoundEffectPlacement`). Defaults to None/None: byte-identical
    # to pre-trim behaviour -- `_compile_sfx_track` plays from the top of the
    # resolved duration, exactly as before this field existed.
    trim_start_s: float | None = Field(default=None, ge=0)
    trim_end_s: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _trim_window(self) -> SubtitledSoundEffect:
        if (
            self.trim_start_s is not None
            and self.trim_end_s is not None
            and self.trim_end_s <= self.trim_start_s
        ):
            raise ValueError("sound effect trim_end_s must be after trim_start_s")
        return self


class SubtitledEndingClip(_LaneModel):
    """An optional Visuals-pool video appended after the speaker clip.

    Muted (``volume=0``) unless the creator asked for its own sound (KRI-841:
    "add the bite video at the very end with its own sound"); the admin lane
    never sets it, so that path stays muted.
    """

    media_id: str = Field(min_length=1, max_length=160, pattern=_ID_PATTERN)
    gcs_path: str = Field(min_length=1)
    generation: str = Field(min_length=1, max_length=160, pattern=_ID_PATTERN)
    trim_start_s: float = Field(default=0.0, ge=0)
    max_duration_s: float | None = Field(default=None, gt=0)
    volume: float = Field(default=0.0, ge=0, le=2)

    @model_serializer(mode="wrap")
    def _muted_shape(self, handler: SerializerFunctionWrapHandler) -> dict:
        # A muted ending keeps the pre-KRI-841 persisted shape (persisted lane
        # rows and the admin request stay byte-identical, and an older reader
        # never meets the new key unless the creator asked for sound).
        payload = handler(self)
        if self.volume == 0:
            payload.pop("volume", None)
        return payload


class SubtitledBackground(_LaneModel):
    """The whole-edit background behind the speaker (KRI-1117).

    One Visuals-pool photo or video, pinned like every other lane visual
    (``gcs_path``/``generation`` are the exact bytes the runner bound). It is a
    whole-edit lane: it has no window, so a speech cleanup cut never moves it, and
    a video loops on output time from ``source_start_s``.
    """

    media_id: str = Field(min_length=1, max_length=160, pattern=_ID_PATTERN)
    gcs_path: str = Field(min_length=1)
    generation: str = Field(min_length=1, max_length=160, pattern=_ID_PATTERN)
    kind: Literal["image", "video"] = "image"
    source_start_s: float = Field(default=0.0, ge=0, le=MAX_BACKGROUND_SOURCE_START_S)

    @model_validator(mode="after")
    def _photo_has_no_timeline(self) -> SubtitledBackground:
        if self.kind == "image" and self.source_start_s != 0:
            raise ValueError("a photo background has no source start")
        return self


def _omit_empty_background(payload: dict) -> dict:
    # Absent while unused: a persisted request or lane state without a background keeps the
    # exact shape (and digest-relevant dump) it had before this lane existed.
    if payload.get("background") is None:
        payload.pop("background", None)
    return payload


class PhoneSubtitledLaneRequest(_LaneModel):
    """The unresolved lane request persisted on
    ``assembly_plan[PHONE_SUBTITLED_LANES_FIELD]``."""

    overlays: list[SubtitledOverlayCard] = Field(default_factory=list)
    sound_effects: list[SubtitledSoundEffect] = Field(default_factory=list)
    ending_clip: SubtitledEndingClip | None = None
    background: SubtitledBackground | None = None

    @model_serializer(mode="wrap")
    def _background_shape(self, handler: SerializerFunctionWrapHandler) -> dict:
        return _omit_empty_background(handler(self))


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
    background: SubtitledBackground | None = None

    @model_serializer(mode="wrap")
    def _background_shape(self, handler: SerializerFunctionWrapHandler) -> dict:
        return _omit_empty_background(handler(self))


_BackgroundCarrier = TypeVar("_BackgroundCarrier", PhoneSubtitledLaneRequest, PhoneSubtitledLanes)


class SubtitledLaneError(UnsupportedPhonePlan):
    """One subtitled lane failed to compile.

    ``lane`` is one of ``"overlays"``, ``"sound_effects"``, ``"ending_clip"``,
    ``"background"`` -- exactly the names `lane_names`/`drop_lane` use -- so the runner can
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
    """Which of ``("overlays", "sound_effects", "ending_clip", "background")`` carry
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
    if lanes.background is not None:
        names.append("background")
    return tuple(names)


def remap_lanes_for_cut(
    lanes: PhoneSubtitledLanes | None, plan: CutPlan
) -> tuple[PhoneSubtitledLanes | None, frozenset[str]]:
    """Shift every lane's main-timeline placement into a `CutPlan`'s cut time.

    Every card/effect here was resolved (grounded or hand-authored) against
    the ORIGINAL source-clip timeline -- grounding samples frames from the
    original clip at those exact timestamps, so it must keep running in
    source time. Once resolved, though, the compiled recipe plays the CUT
    timeline, so this is the one place their placement moves:

      - ``overlays``: a card's ``start_s``/``end_s`` window is remapped with
        `app.pipeline.silence_cut.remap_range`; a card whose ENTIRE window
        lands inside one or more removals is dropped (its id is returned so
        the caller can demote its grounding/beat receipt entry instead of
        silently losing it). ``source_start_s`` (a video card's OWN offset
        into ITS OWN source file) is untouched -- that is never a
        main-timeline coordinate.
      - ``sound_effects``: only the request's ``at_s`` placement moves, via
        `app.pipeline.silence_cut.remap_time` -- never dropped, since an
        effect has no fixed on-timeline end to test against a removal (its
        played duration is a property of the resolved catalog asset, not a
        window here).
      - ``ending_clip``: untouched. It always starts right after the
        speaker clip's OWN (now-shorter) end, never at a fixed
        source-timeline position, so it needs no remap.
      - ``background``: untouched. It is a whole-edit lane with no window, and
        a video keeps looping on output time, so a cut moves nothing in it.

    Returns ``(lanes, frozenset())`` unchanged when ``lanes`` is ``None`` or
    the plan removed nothing (byte-identical fast path for every render that
    never applies a cut).

    No frame grid: the phone recipe places each keep segment at its exact
    float offsets (`compile_phone_subtitled_plan`), unlike the cloud cut.
    """
    if lanes is None or not plan.removed:
        return lanes, frozenset()
    from app.pipeline.silence_cut import remap_range, remap_time  # noqa: PLC0415

    kept_cards: list[SubtitledOverlayCard] = []
    dropped_card_ids: set[str] = set()
    for card in lanes.overlays:
        mapped = remap_range(card.start_s, card.end_s, plan)
        if mapped is None:
            dropped_card_ids.add(card.id)
            continue
        new_start, new_end = mapped
        kept_cards.append(card.model_copy(update={"start_s": new_start, "end_s": new_end}))

    remapped_sfx: list[ResolvedSoundEffect] = []
    for resolved in lanes.sound_effects:
        new_at = remap_time(resolved.request.at_s, plan)
        remapped_sfx.append(
            resolved.model_copy(
                update={"request": resolved.request.model_copy(update={"at_s": new_at})}
            )
        )
    remapped = lanes.model_copy(update={"overlays": kept_cards, "sound_effects": remapped_sfx})
    return remapped, frozenset(dropped_card_ids)


SPEAKER_BACKGROUND_FALLBACK_EVENT = "speaker_background_phone_fallback"


def strip_unsupported_background(lanes: _BackgroundCarrier | None) -> _BackgroundCarrier | None:
    """``lanes`` without its background while the phone gate is closed (KRI-1117).

    Closed = `phone_rollout.speaker_background_unavailable_reason`: kill switch off,
    ``speakerBackground`` not verified, or (inside an app request) a build below the
    minimum protocol. The lane is removed BEFORE anything is bound or compiled, so the
    recipe is byte-identical to one for a request that never asked, and the creator
    still gets the edit (with their own background). The fallback is recorded for the
    job-debug view. Works on the resolved lanes and on the persisted request alike.
    """
    if lanes is None or lanes.background is None:
        return lanes
    from app.services.phone_rollout import speaker_background_unavailable_reason  # noqa: PLC0415

    reason = speaker_background_unavailable_reason()
    if reason is None:
        return lanes
    import structlog  # noqa: PLC0415

    from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415

    structlog.get_logger(__name__).info(
        SPEAKER_BACKGROUND_FALLBACK_EVENT, media_id=lanes.background.media_id, reason=reason
    )
    record_pipeline_event(
        "phone",
        SPEAKER_BACKGROUND_FALLBACK_EVENT,
        {"media_id": lanes.background.media_id, "reason": reason},
    )
    return lanes.model_copy(update={"background": None})


def drop_lane(lanes: PhoneSubtitledLanes, name: str) -> PhoneSubtitledLanes:
    """A copy of ``lanes`` with lane ``name`` emptied, so the runner can
    retry a compile without the lane that just failed."""
    if name == "overlays":
        return lanes.model_copy(update={"overlays": []})
    if name == "sound_effects":
        return lanes.model_copy(update={"sound_effects": []})
    if name == "ending_clip":
        return lanes.model_copy(update={"ending_clip": None})
    if name == "background":
        return lanes.model_copy(update={"background": None})
    raise ValueError(f"unknown subtitled lane: {name}")
