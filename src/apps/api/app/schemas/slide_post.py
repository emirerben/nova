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

import uuid
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_serializer, model_validator

from app.kria.brief_binding import BriefBinding
from app.pipeline.look_presets import LookPreset
from app.pipeline.slide_post.profiles import PlatformProfile

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
    "stroke_color",
    "shadow_color",
    "shadow_opacity",
    "background_color",
    "editor_preset",
    "text_case",
    "letter_spacing",
    "line_spacing",
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
    stroke_color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    shadow_color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    shadow_opacity: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    # Wins over the legacy `background` box when set (native "Highlight" preset).
    background_color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    editor_preset: Literal["Simple", "Bold", "Highlight"] | None = None
    text_case: Literal["none", "upper", "lower", "title"] | None = None
    letter_spacing: float | None = None  # em, clamped to [-0.05, 0.5]
    line_spacing: float | None = None  # multiplier, clamped to [0.5, 3.0]

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

    @model_validator(mode="after")
    def _validate_cover_and_ids(self) -> SlidePostDraft:
        if self.slides:
            if self.cover_index >= len(self.slides):
                raise ValueError("cover_index out of range")
            ids = [s.id for s in self.slides]
            if len(set(ids)) != len(ids):
                raise ValueError("slide ids must be unique within a draft")
        elif self.cover_index != 0:
            raise ValueError("cover_index must be 0 when there are no slides")
        return self


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
            }
        )
        merged.append(ref.model_copy(update={"edits": new_edits}))
    return merged
