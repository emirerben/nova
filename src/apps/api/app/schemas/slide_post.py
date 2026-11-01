"""Pydantic schema for `PlanItem.slide_post` — the reviewable mixed-media
"slide post" draft (an ordered sequence of images and videos, e.g. a TikTok
photo-mode post or an Instagram carousel).

Deliberately a SEPARATE column and a separate, lighter-weight envelope from
`app/schemas/edit_proposal.py`, even though both are "draft + approval state
+ staleness" documents (plans/024 eng-review). Reusing `edit_proposal` would
entangle a slide draft with `guided_edit_applicable`'s media-sync/staleness
machinery, which exists specifically for the guided-story renderer — a slide
post must never be able to trip that path. What IS borrowed from
`edit_proposal` is the *shape* of the verbs: a monotonic `version` on the
draft, a `rendered_version` marker recording which version the current
render was built from (the `approved`/staleness signal), and a fail-closed
`parse_slide_post` matching `parse_edit_proposal`.

A slide never carries a raw GCS path — it references a `PlanItemAsset.id`.
The asset row (already promoted, already analyzed) is the source of truth
for the actual media; see `app/pipeline/slide_post/build.py` for how a slide
is resolved to bytes at render time.
"""

from __future__ import annotations

import copy
import uuid
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_serializer,
    model_validator,
)

from app.agents._schemas.drawing import (
    MAX_POINTS_PER_DRAWING,
    MAX_POINTS_PER_VARIANT,
    MAX_STROKES_PER_DRAWING,
    DrawingStroke,
)
from app.kria.brief_binding import BriefBinding
from app.kria.recipes import NormalizedSourceCrop
from app.pipeline.look_presets import LookPreset
from app.pipeline.slide_post.profiles import PlatformProfile, SlideAspect, coerce_aspect

# Hard outer bound independent of platform profile — the tightest profile
# (tiktok_photo, 35) is looser than this only by convention; this cap exists
# so a malformed/oversized draft can never reach the profile validator or the
# renderer. Profile-specific limits (e.g. instagram_carousel's 20) are
# enforced by `app.pipeline.slide_post.profiles.validate`, not here.
MAX_SLIDES = 35

# v1 per-slide editing is deliberately narrow (plans/024 follow-up eng-review,
# 2026-09-10): one optional text overlay in one of three fixed positions, plus
# a look preset. No free-drag positioning, no stacked overlays, no Overlays/
# Captions/Sounds — those need pipelines not yet verified reusable outside a
# rendered Job (see the plan's Decision 2). Cap text length so a burned
# drawtext overlay can never overflow the canvas unpredictably.
MAX_SLIDE_TEXT_LENGTH = 120


class TextOverlay(BaseModel):
    """One static text overlay burned onto a single slide via FFmpeg drawtext."""

    content: str = Field(min_length=1, max_length=MAX_SLIDE_TEXT_LENGTH)
    position: str = Field(pattern="^(top|center|bottom)$")


MAX_SLIDE_TEXTS = 4
DEFAULT_SLIDE_TEXT_FONT = "Inter-Bold"
# Pixel size of the legacy drawtext overlay on the 1920-tall canvas
# (round(1920 * 0.045)); used when a legacy `text` is lifted into an element.
_LEGACY_TEXT_SIZE_PX = 86

# Style fields added for video-editor Text-tool parity. All optional and
# omitted from the serialized form while None, so a pre-parity element hashes
# (`edits_cache_digest`) and serializes exactly as it did before they existed.
_PARITY_STYLE_FIELDS = (
    "rotation_deg",
    "bend",
    "stroke_color",
    "shadow_color",
    "shadow_opacity",
    "background_color",
    "editor_preset",
    "text_case",
    "letter_spacing",
    "line_spacing",
    "wrap_lines",
)


class SlideTextElement(BaseModel):
    """One styled text element on a single slide (rich text model, KRI-298).

    Slides are NOT a scaled-down video-editor document (no timing, animation,
    captions, sounds or overlays), but the text style vocabulary is now shared
    with the video editor's Text tool.

    The style vocabulary is shared with `agents/_schemas/text_element.TextElement`
    (the video editor's Text tool): validators and bounds for the parity fields
    are reused from it, not copied. Timing, animation, behind-subject and
    word-highlight are intentionally absent: they mean nothing on a still.
    Rendered by `pipeline/slide_post/build.py` through the Pillow
    `text_overlay._draw_text_png` path (PNG overlay) when
    `slide_post_rich_text_enabled` is on. All style fields default so a
    partial payload from a client stays valid.
    """

    id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=MAX_SLIDE_TEXT_LENGTH)
    # `label` marks machine-authored place/time captions (chat "add each
    # photo's location"); `edited` records that the user changed one so a
    # re-label never overwrites it.
    role: Literal["text", "label"] = "text"
    label_source: Literal["place", "capture_time"] | None = None
    edited: bool = False
    font_family: str = DEFAULT_SLIDE_TEXT_FONT
    color: str = "#FFFFFF"
    # int|float: ints stay ints (legacy payloads/digests unchanged); native
    # editor sizes may be fractional and go below the old 24 floor.
    size_px: int | float = Field(default=_LEGACY_TEXT_SIZE_PX, ge=8, le=200, allow_inf_nan=False)
    alignment: Literal["left", "center", "right"] = "center"
    position: Literal["top", "center", "bottom", "custom"] = "bottom"
    # Fractions of the SLIDE canvas (so 4:5 slides place identically to the
    # client's preview). Only read when position == "custom"; x_frac is the
    # centerline for center alignment and the edge for left/right.
    x_frac: float | None = Field(default=None, ge=0.0, le=1.0)
    y_frac: float | None = Field(default=None, ge=0.0, le=1.0)
    max_width_frac: float | None = Field(default=None, ge=0.2, le=1.0)
    stroke_width: int | float = Field(default=0, ge=0, le=20, allow_inf_nan=False)
    shadow_enabled: bool = True
    background: Literal["none", "box"] = "none"
    # --- text-tool parity fields (all optional; None = renderer default) ---
    rotation_deg: float | None = None  # clockwise, clamped to [-360, 360]
    # KRI-646: curve in [-100, 100] (+ = rainbow). Drawn on device only; the
    # server stores it and never renders it. Needs the `text_bend` capability.
    bend: float | None = None
    stroke_color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    shadow_color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    shadow_opacity: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    # Wins over the legacy `background` box when set (native "Highlight" preset).
    background_color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    editor_preset: Literal["Simple", "Bold", "Highlight"] | None = None
    text_case: Literal["none", "upper", "lower", "title"] | None = None
    letter_spacing: float | None = None  # em, clamped to [-0.05, 0.5]
    line_spacing: float | None = None  # multiplier, clamped to [0.5, 3.0]
    # False = only explicit newlines break lines (the video editor's KRI-508 contract);
    # None = legacy auto-wrap at max_width_frac.
    wrap_lines: bool | None = None

    @model_serializer(mode="wrap")
    def _omit_unset_parity_fields(self, handler):
        payload = handler(self)
        for key in _PARITY_STYLE_FIELDS:
            if payload.get(key) is None:
                payload.pop(key, None)
        return payload

    @field_validator("rotation_deg", mode="before")
    @classmethod
    def _clamp_rotation(cls, value: object) -> float | None:
        from app.agents._schemas.text_element import TextElement  # noqa: PLC0415

        return TextElement._clamp_rotation_deg(value)

    @field_validator("bend", mode="before")
    @classmethod
    def _clamp_bend(cls, value: object) -> float | None:
        from app.agents._schemas.text_element import TextElement  # noqa: PLC0415

        return TextElement._clamp_bend(value)

    @field_validator("letter_spacing", mode="before")
    @classmethod
    def _clamp_letter(cls, value: object) -> float | None:
        from app.agents._schemas.text_element import TextElement  # noqa: PLC0415

        return TextElement._clamp_letter_spacing(value)

    @field_validator("line_spacing", mode="before")
    @classmethod
    def _clamp_line(cls, value: object) -> float | None:
        from app.agents._schemas.text_element import TextElement  # noqa: PLC0415

        return TextElement._clamp_line_spacing(value)

    @field_validator("text_case", mode="before")
    @classmethod
    def _coerce_case(cls, value: object) -> str | None:
        from app.agents._schemas.text_element import TextElement  # noqa: PLC0415

        return TextElement._coerce_text_case(value)

    @field_validator("font_family")
    @classmethod
    def _font_allowed(cls, value: str) -> str:
        from app.agents._schemas.text_element import _ALLOWED_FONTS  # noqa: PLC0415

        if value not in _ALLOWED_FONTS:
            raise ValueError(f"Unknown font_family {value!r}")
        return value

    @field_validator("color")
    @classmethod
    def _color_hex(cls, value: str) -> str:
        from app.agents._schemas.text_element import _HEX_COLOR_RE  # noqa: PLC0415

        if not _HEX_COLOR_RE.match(value):
            raise ValueError("color must be #RRGGBB")
        return value


def _position_bucket(element: SlideTextElement) -> str:
    """Nearest legacy top/center/bottom bucket for an element."""
    if element.position != "custom":
        return element.position
    y = 0.5 if element.y_frac is None else element.y_frac
    if y < 0.33:
        return "top"
    if y > 0.66:
        return "bottom"
    return "center"


SlideOrientation = Literal["portrait", "landscape"]

# How a photo sits in the post canvas (KRI-606). None = fill (cover-crop, the
# historical behaviour); a value shows the whole (cropped) photo with these bars.
SlideFit = Literal["blur", "black", "white"]


class SlideFraming(BaseModel):
    """How this slide's media is framed (KRI-44).

    ``orientation`` picks the slide's own export canvas ("landscape" =
    1920x1080, TikTok photo profile only). ``source_crop`` is a normalized,
    top-left-origin rectangle in the UPRIGHT (display) frame of the source,
    cover-fitted into that canvas — the same space as ``NormalizedSourceCrop``
    so device and cloud agree. See ``app/kria/framing.py``.
    """

    orientation: SlideOrientation = "portrait"
    source_crop: NormalizedSourceCrop | None = None


class SlideDrawing(BaseModel):
    """A static freehand drawing over one slide (KRI-589 PR2).

    Strokes only (the shared ``DrawingStroke``), never a raster: every renderer
    paints them (iOS ``NativeDrawingPainter``, the server's ``drawing_png``).
    Coordinates are normalised to the authoring canvas (``canvas_aspect`` =
    width / height); a renderer aspect-fits that canvas into the slide's output
    canvas, so a slide whose shape later changes letterboxes the ink instead of
    stretching it. No scale/rotation/centre/timing: a slide drawing is the
    identity transform of a still. Frozen by
    ``tests/fixtures/slide-drawing-parity/v1.json``.
    """

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    canvas_aspect: float = Field(gt=0.2, lt=5.0)
    strokes: list[DrawingStroke] = Field(min_length=1, max_length=MAX_STROKES_PER_DRAWING)

    @model_validator(mode="after")
    def _point_budget(self) -> SlideDrawing:
        if self.point_count() > MAX_POINTS_PER_DRAWING:
            raise ValueError(f"a slide drawing holds at most {MAX_POINTS_PER_DRAWING} points")
        return self

    def point_count(self) -> int:
        return sum(len(stroke.points) for stroke in self.strokes)


class SlideTiltBackdrop(BaseModel):
    """What shows behind a tilted slide where the media no longer covers the canvas (KRI-1138).

    The slide twin of ``app.kria.recipes.ClipBackdrop`` and ``KriaMediaEngine.ClipBackdrop``:
    ``blank`` is black, ``color`` carries a ``#RRGGBB``, ``blur`` is a blurred cover-fit copy of
    the same photo or video frame (the Fit "blur" bars' strength, ``FIT_BLUR_SIGMA_PX``).
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["blank", "color", "blur"] = "blank"
    color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")

    @model_validator(mode="after")
    def _color_only_for_color(self) -> SlideTiltBackdrop:
        if (self.kind == "color") != (self.color is not None):
            raise ValueError("a backdrop carries a color if and only if its kind is color")
        return self

    @model_serializer(mode="wrap")
    def _omit_unused_color(self, handler: Any) -> dict[str, Any]:
        data = handler(self)
        if isinstance(data, dict) and data.get("color") is None:
            data.pop("color", None)
        return data


class SlideTilt(BaseModel):
    """A slide's media turned to any angle about the canvas centre (KRI-1138).

    ``rotation_deg`` is clockwise-positive in -180..180 (the clip tilt, sticker and text
    convention). Only the PHOTO or VIDEO turns: text, ink, stickers and the watermark stay
    upright on top. What the turn uncovers shows ``backdrop`` (default blank = black). Frozen
    by ``tests/fixtures/slide-tilt-parity/v1.json``.
    """

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    rotation_deg: float = Field(ge=-180.0, le=180.0)
    backdrop: SlideTiltBackdrop = Field(default_factory=SlideTiltBackdrop)


MAX_SLIDE_STICKERS = 4


class SlideSticker(BaseModel):
    """One pasted image sticker on a slide (KRI-647, KRI-460).

    ``asset_id`` is a ``PlanItemAsset`` (image) in the item's pool that is NEVER a
    slide. Geometry is relative to the slide's OUTPUT canvas: ``x_frac``/``y_frac``
    are the sticker CENTRE as fractions of canvas width/height, ``width_frac`` the
    displayed width as a fraction of canvas width (height follows the image's own
    aspect, never distorted) and ``rotation_deg`` is clockwise about the centre.
    Stickers are clipped by the canvas edge, never moved. Frozen by
    ``tests/fixtures/slide-sticker-parity/v1.json``.
    """

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    id: str = Field(min_length=1, max_length=64)
    asset_id: str = Field(min_length=1, max_length=64)
    x_frac: float = Field(ge=0.0, le=1.0)
    y_frac: float = Field(ge=0.0, le=1.0)
    width_frac: float = Field(ge=0.05, le=1.0)
    rotation_deg: float = Field(default=0.0, ge=-180.0, le=180.0)


class SlideEdits(BaseModel):
    """Per-slide edit state, applied at render time in `pipeline/slide_post/build.py`.

    `text` is the legacy single drawtext overlay. `texts` is the rich model
    (max 4 styled elements). When `texts` is not None, `text` is ALWAYS a
    mirror of `texts[0]` (None when `texts` is empty) so old clients and the
    flag-off render path keep working. `look_preset` maps to an FFmpeg filter
    fragment; there is no separate rendering subsystem for it.
    """

    text: TextOverlay | None = None
    look_preset: LookPreset = "none"
    texts: list[SlideTextElement] | None = Field(default=None, max_length=MAX_SLIDE_TEXTS)
    # Omitted from the serialized form while None (see `_omit_unset_framing`) so
    # an unframed slide hashes/serializes exactly as before this field existed.
    framing: SlideFraming | None = None
    # A sibling of `framing`, not nested in it: the KRI-44 iOS build re-encodes
    # `framing` from its own struct and would drop an unknown nested key, whereas
    # an unknown sibling key is simply omitted by that build (and the server's
    # "missing means keep" merge protects it). Omitted while None for the same
    # cache-digest reason as `framing`.
    fit: SlideFit | None = None
    # Static freehand ink (KRI-589 PR2). Omitted while None for the same
    # digest/binding-stability reason as `framing`/`fit`; a sibling key so an
    # older build that drops it on re-encode is protected by the PUT merge.
    drawing: SlideDrawing | None = None
    # Pasted image stickers (KRI-647), list order = z-order (last on top). Omitted while
    # None or empty so a slide without stickers serializes and hashes exactly as before.
    stickers: list[SlideSticker] | None = Field(default=None, max_length=MAX_SLIDE_STICKERS)
    # The media turned to any angle (KRI-1138). A sibling of `framing` for the same reason as
    # `fit`; omitted while None, and an upright slide is None (see `_upright_is_not_a_tilt`).
    tilt: SlideTilt | None = None

    @model_serializer(mode="wrap")
    def _omit_unset_framing(self, handler: Any) -> dict[str, Any]:
        data = handler(self)
        if isinstance(data, dict):
            for key in ("framing", "fit", "drawing", "tilt"):
                if data.get(key) is None:
                    data.pop(key, None)
            if not data.get("stickers"):
                data.pop("stickers", None)
        return data

    @field_validator("stickers", mode="before")
    @classmethod
    def _sticker_count(cls, value: object) -> object:
        if isinstance(value, list) and len(value) > MAX_SLIDE_STICKERS:
            raise ValueError(f"too many stickers on one slide (at most {MAX_SLIDE_STICKERS})")
        return value

    @model_validator(mode="after")
    def _upright_is_not_a_tilt(self) -> SlideEdits:
        # 0 degrees draws nothing, so storing it would only fork the cache key of an
        # unchanged slide and leave a backdrop that never shows.
        if self.tilt is not None and self.tilt.rotation_deg == 0:
            self.tilt = None
        return self

    @model_validator(mode="after")
    def _unique_sticker_ids(self) -> SlideEdits:
        if self.stickers:
            ids = [s.id for s in self.stickers]
            if len(set(ids)) != len(ids):
                raise ValueError("sticker ids must be unique within a slide (duplicate id)")
        return self

    @model_validator(mode="after")
    def _mirror_texts_into_legacy_text(self) -> SlideEdits:
        if self.texts is None:
            return self
        ids = [t.id for t in self.texts]
        if len(set(ids)) != len(ids):
            raise ValueError("text ids must be unique within a slide")
        if self.texts:
            first = self.texts[0]
            self.text = TextOverlay(content=first.text, position=_position_bucket(first))
        else:
            self.text = None
        return self

    def effective_texts(self) -> list[SlideTextElement]:
        """`texts` when set, else the legacy `text` lifted into one element
        (boxed, white, bottom/top/center) so every consumer sees one shape."""
        if self.texts is not None:
            return list(self.texts)
        if self.text is None:
            return []
        return [
            SlideTextElement(
                id="legacy",
                text=self.text.content,
                position=self.text.position,  # type: ignore[arg-type]
                background="box",
                shadow_enabled=False,
            )
        ]


class SlideRef(BaseModel):
    """One item in the ordered sequence. Points at pooled media, never copies it."""

    # Client-stable id (not the asset id) so the frontend can key a reorder
    # without re-fetching — minted once when the slide is added to the draft.
    id: str = Field(min_length=1, max_length=64)
    asset_id: uuid.UUID
    kind: str = Field(pattern="^(image|video)$")
    # Per-slide accessibility / caption text. Optional — the composer agent
    # fills it; the user may edit or clear it.
    alt: str | None = Field(default=None, max_length=500)
    # None means "unedited" — must stay distinguishable from
    # SlideEdits(text=None, look_preset="none") so the render-cache key (see
    # generative_build.py's slide normalize loop) doesn't have to special-case
    # an all-default SlideEdits differently from no edits at all; both hash
    # the same way in practice, but None is the honest default.
    edits: SlideEdits | None = None


def _portrait_slide(slide: SlideRef) -> SlideRef:
    """A slide that was landscape goes back to the post's shape, crop and all: a
    rectangle chosen for 16:9 would be mis-shaped for 4:5 (the iPhone does the same)."""
    framing = slide.edits.framing if slide.edits else None
    if framing is None or framing.orientation == "portrait":
        return slide
    assert slide.edits is not None
    return slide.model_copy(update={"edits": slide.edits.model_copy(update={"framing": None})})


def _without_video_fit(slide: SlideRef) -> SlideRef:
    """Fit is a photo-only treatment: a video slide never carries it."""
    if slide.kind != "video" or slide.edits is None or slide.edits.fit is None:
        return slide
    return slide.model_copy(update={"edits": slide.edits.model_copy(update={"fit": None})})


def drawing_point_total(slides: list[SlideRef]) -> int:
    return sum(
        s.edits.drawing.point_count() for s in slides if s.edits is not None and s.edits.drawing
    )


def sticker_asset_ids(draft: SlidePostDraft | None) -> set[uuid.UUID]:
    """Every pool asset id any slide of ``draft`` uses as a sticker (never a slide).

    A malformed id (stored by nothing we wrote, but the field is a plain string on the
    wire) is skipped rather than raising, so a bad value can never break a read path.
    """
    ids: set[uuid.UUID] = set()
    if draft is None:
        return ids
    for slide in draft.slides:
        for sticker in (slide.edits.stickers or []) if slide.edits is not None else []:
            try:
                ids.add(uuid.UUID(sticker.asset_id))
            except ValueError:
                continue
    return ids


class SlidePostDraft(BaseModel):
    """The persisted draft document (`PlanItem.slide_post`)."""

    schema_version: int = 1
    # Bumped on every edit (reorder, add, remove, cover/caption/profile
    # change). Compared against `rendered_version` to decide staleness —
    # mirrors `voiceover_script.version` vs `voiceover_script_recorded_version`.
    version: int = Field(ge=1, default=1)
    platform_profile: PlatformProfile
    slides: list[SlideRef] = Field(default_factory=list, max_length=MAX_SLIDES)
    cover_index: int = Field(ge=0, default=0)
    caption: str = Field(default="", max_length=2200)  # Instagram's own cap
    # The `version` the currently-rendered `slides` variant was built from.
    # None before the first successful render. draft is stale (needs a
    # rebuild) whenever `version != rendered_version`.
    rendered_version: int | None = None
    # False for an untouched AI (composer) draft; True the moment the user
    # changes anything. Display-only — never gates rendering.
    user_edited: bool = False
    # Immutable creator-request/asset identity authority for follow-up chat
    # edits. Optional keeps legacy draft readers and rollback payloads valid.
    brief_binding: BriefBinding | None = None
    # Post-wide size (KRI-606). None = the profile's default size; omitted from
    # the serialized draft while None so existing drafts, their brief-binding
    # snapshot digests and render cache keys are byte-identical.
    aspect: SlideAspect | None = None

    @model_serializer(mode="wrap")
    def _omit_unset_aspect(self, handler: Any) -> dict[str, Any]:
        data = handler(self)
        if isinstance(data, dict) and data.get("aspect") is None:
            data.pop("aspect", None)
        return data

    @field_validator("brief_binding", mode="before")
    @classmethod
    def _canonicalize_binding_snapshot(cls, value: object) -> object:
        return canonicalize_slide_post_brief_binding(value)

    @model_validator(mode="after")
    def _validate_cover_and_ids(self) -> SlidePostDraft:
        # An aspect the profile doesn't allow (or its default) collapses to None,
        # so switching platform falls back to the new platform's default size.
        # Assign only on a real change: assignment adds the field to `model_fields_set`,
        # and the chat-edit backfill reads that set to tell "client omitted it" apart
        # from "client sent it".
        coerced = coerce_aspect(self.platform_profile, self.aspect)
        if coerced != self.aspect:
            self.aspect = coerced
        if self.slides:
            if self.cover_index >= len(self.slides):
                raise ValueError("cover_index out of range")
            ids = [s.id for s in self.slides]
            if len(set(ids)) != len(ids):
                raise ValueError("slide ids must be unique within a draft")
            if self.platform_profile == "instagram_carousel":
                # Instagram forces one shape for every slide, so a slide that was
                # landscape on the TikTok profile falls back to the post's shape.
                self.slides = [_portrait_slide(s) for s in self.slides]
            # Video slides always cover-fill the post canvas.
            self.slides = [_without_video_fit(s) for s in self.slides]
        elif self.cover_index != 0:
            raise ValueError("cover_index must be 0 when there are no slides")
        if drawing_point_total(self.slides) > MAX_POINTS_PER_VARIANT:
            raise ValueError(f"drawings exceed {MAX_POINTS_PER_VARIANT} points in total")
        return self


def canonicalize_slide_post_brief_binding(value: object) -> object:
    """Normalize a client-round-tripped slide snapshot before digest verification.

    Native JSON decoding represents every number as ``Double``.  A server-minted
    snapshot can therefore return with equivalent values such as ``1`` in place
    of a schema's ``1.0``.  Canonicalize only the embedded slide draft so the
    binding retains its original server digest; all other binding fields remain
    covered by the normal immutable binding validation.
    """
    if not isinstance(value, dict):
        return value

    payload: dict[str, Any] = copy.deepcopy(value)
    snapshot = payload.get("media_snapshot")
    if not isinstance(snapshot, dict):
        return value
    snapshot_draft = snapshot.get("draft")
    if not isinstance(snapshot_draft, dict):
        return value
    try:
        draft = SlidePostDraft.model_validate(snapshot_draft)
    except ValidationError:
        return value
    snapshot["draft"] = draft.model_dump(mode="json", exclude={"brief_binding"})
    return payload


def parse_slide_post(value: object) -> SlidePostDraft | None:
    """Fail closed for legacy/corrupt JSONB instead of breaking item reads."""

    if not isinstance(value, dict):
        return None
    try:
        return SlidePostDraft.model_validate(value)
    except Exception:  # noqa: BLE001 - corrupted JSONB is treated as no draft
        return None


def slide_post_needs_render(draft: SlidePostDraft) -> bool:
    """True when the draft has changed since the last successful render."""

    return draft.rendered_version != draft.version


def mark_slide_post_rendered(draft: SlidePostDraft) -> SlidePostDraft:
    """Stamp the draft as rendered at its current version (post-render write)."""

    return draft.model_copy(update={"rendered_version": draft.version})


def bump_slide_post_version(draft: SlidePostDraft, **updates: object) -> SlidePostDraft:
    """Apply `updates` and mark the draft user-edited at the next version.

    Single mutation point for every draft edit (reorder/add/remove/cover/
    caption/profile) so `version` and `user_edited` can never drift from an
    actual change, mirroring how `plan_clips.set_item_clips` is the single
    writer for `clip_gcs_paths`.
    """

    # ``model_copy(update=...)`` deliberately skips Pydantic validation.  A
    # route using it for client supplied slides could otherwise persist an
    # invalid cover index or duplicate stable slide id.  Revalidate the full
    # JSON shape at this persistence boundary.
    return SlidePostDraft.model_validate(
        draft.model_dump(mode="python")
        | {**updates, "version": draft.version + 1, "user_edited": True}
    )


def merge_legacy_text_edits(
    stored: SlidePostDraft | None, slides: list[SlideRef]
) -> list[SlideRef]:
    """Protect stored rich `texts` from an old client's PUT.

    A client that predates `SlideEdits.texts` decodes a slide, drops the
    unknown key and writes `edits` back with `texts` omitted. Omission (checked
    via `model_fields_set`) means "unchanged", not "cleared": keep the stored
    texts. If the legacy mirror `text` changed, fold that change into
    `texts[0]` so the old client's edit still lands. An explicit
    `texts: null`/list from a new client is taken as sent.
    """
    if stored is None:
        return slides
    stored_by_id = {s.id: s for s in stored.slides}
    merged: list[SlideRef] = []
    for ref in slides:
        old = stored_by_id.get(ref.id)
        incoming = ref.edits
        if (
            incoming is None
            or "texts" in incoming.model_fields_set
            or old is None
            or old.edits is None
            or old.edits.texts is None
        ):
            merged.append(ref)
            continue
        texts = list(old.edits.texts)
        if incoming.text != old.edits.text:
            if incoming.text is None:
                texts = texts[1:]
            elif texts:
                head = texts[0]
                update: dict[str, object] = {"text": incoming.text.content, "edited": True}
                if incoming.text.position != _position_bucket(head):
                    update.update(position=incoming.text.position, x_frac=None, y_frac=None)
                texts[0] = head.model_copy(update=update)
            else:
                texts = [
                    SlideTextElement(
                        id=uuid.uuid4().hex[:12],
                        text=incoming.text.content,
                        position=incoming.text.position,  # type: ignore[arg-type]
                        background="box",
                        shadow_enabled=False,
                    )
                ]
        new_edits = SlideEdits.model_validate(
            {
                "text": None,
                "look_preset": incoming.look_preset,
                "texts": [t.model_dump(mode="python") for t in texts],
                **(
                    {
                        "framing": incoming.framing.model_dump(mode="python")
                        if incoming.framing is not None
                        else None
                    }
                    if "framing" in incoming.model_fields_set
                    else {}
                ),
                # Only when sent: passing fit=None unconditionally would mark it
                # explicit and the later merge would clear a stored Fit (the web
                # editor has no `texts`, so every web save takes this path).
                **({"fit": incoming.fit} if "fit" in incoming.model_fields_set else {}),
                # Same rule for drawings (KRI-589): only when sent.
                **(
                    {
                        "drawing": incoming.drawing.model_dump(mode="python")
                        if incoming.drawing is not None
                        else None
                    }
                    if "drawing" in incoming.model_fields_set
                    else {}
                ),
                # ...and for stickers (KRI-647): only when sent.
                **(
                    {
                        "stickers": [s.model_dump(mode="python") for s in incoming.stickers]
                        if incoming.stickers is not None
                        else None
                    }
                    if "stickers" in incoming.model_fields_set
                    else {}
                ),
                # ...and for tilt (KRI-1138): only when sent.
                **(
                    {
                        "tilt": incoming.tilt.model_dump(mode="python")
                        if incoming.tilt is not None
                        else None
                    }
                    if "tilt" in incoming.model_fields_set
                    else {}
                ),
            }
        )
        merged.append(ref.model_copy(update={"edits": new_edits}))
    return merged


def merge_stored_edit_key(
    stored: SlidePostDraft | None,
    slides: list[SlideRef],
    key: Literal["framing", "fit", "drawing", "stickers", "tilt"],
    *,
    accept_incoming: bool,
) -> list[SlideRef]:
    """Protect a stored per-slide edit key from a client that omits it.

    Three states, decided per slide via `model_fields_set`:
    - key missing (an older iOS build, the web editor, or `edits: null`)
      -> keep what is stored.
    - explicit `null` -> cleared.
    - a value -> set as sent.
    With ``accept_incoming=False`` (the key's feature flag off) every slide
    keeps its stored value, so no such draft exists until api and worker both
    run the new code.
    """
    stored_by_id = {s.id: s for s in stored.slides} if stored is not None else {}
    merged: list[SlideRef] = []
    for ref in slides:
        old = stored_by_id.get(ref.id)
        old_value = getattr(old.edits, key) if old is not None and old.edits is not None else None
        incoming = ref.edits
        explicit = accept_incoming and incoming is not None and key in incoming.model_fields_set
        if explicit:
            merged.append(ref)
            continue
        current = getattr(incoming, key) if incoming is not None else None
        if old_value == current:
            merged.append(ref)
            continue
        if incoming is None:
            edits = SlideEdits(**{key: old_value})
        else:
            edits = incoming.model_copy(update={key: old_value})
        merged.append(ref.model_copy(update={"edits": edits}))
    return merged


def merge_stored_framing(
    stored: SlidePostDraft | None, slides: list[SlideRef], *, accept_incoming: bool
) -> list[SlideRef]:
    """Protect stored `framing` (crop/orientation) — see `merge_stored_edit_key`."""
    return merge_stored_edit_key(stored, slides, "framing", accept_incoming=accept_incoming)
