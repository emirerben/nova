"""Typed, execution-free contract for phone-rendered reaction beats (KRI-178).

A reaction beat is a creator-authored rule: "when you hear me say <trigger>,
pop up <visual_id> (a photo or sticker the creator already owns) and/or play
<sound> (a catalog sound effect or the creator's own description)". A worker
grounds each beat against the real transcript at render time -- this module
only carries the creator's REQUEST, never a resolved timestamp, matching how
``app.schemas.clip_intents.ClipIntent`` separates the open-vocabulary request
from its resolution.

Both ``visual_id`` here and ``ClosingMedia.visual_id``/``badge_visual_id`` are
opaque manifest media ids -- exactly the ids ``ResolvedCreatorManifest.media``
already exposes for the creator's own images. Resolving them against the real
pool (and dropping anything that doesn't resolve to an owned image) is
``app.services.creator_capabilities.compile_strategy_to_plan``'s job, not
this module's.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_REACTION_BEATS = 24

BeatVisualRole = Literal["photo", "sticker"]


def _clean(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = " ".join(value.split())
    return stripped or None


class ReactionBeat(BaseModel):
    """One name/word-triggered pop-in: a photo/sticker and/or a sound effect,
    fired the (first or every) time the creator says ``trigger`` -- optionally
    only counting an occurrence AFTER ``after`` is heard ("no" after "Mason
    Greenwood")."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    beat_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,40}$")
    # The creator's exact spoken words to listen for, in their own language
    # ("Mason Greenwood", "number three", "hayır" / "no").
    trigger: str = Field(min_length=1, max_length=80)
    # Only count an occurrence of `trigger` spoken AFTER this phrase is heard
    # ("no" after "Mason Greenwood" or "Vlahović").
    after: str | None = Field(default=None, max_length=80)
    occurrence: Literal["first", "every"] = "first"
    # An owned IMAGE media_id from the manifest (photo or sticker). Resolved
    # (label/media_id match, unknown -> dropped) by
    # `app.services.creator_capabilities.compile_strategy_to_plan`.
    visual_id: str | None = Field(default=None, max_length=200)
    visual_role: BeatVisualRole = "sticker"
    # A manifest sound_effect catalog_id, OR the creator's own description
    # ("buzzer", "ding") when no catalog entry matches -- the worker resolves
    # a description via `_resolve_phone_sound_effect`.
    sound: str | None = Field(default=None, max_length=80)
    # Seconds held on screen; None lets the server pick its own default.
    hold_s: float | None = Field(default=None, ge=0.5, le=8.0)

    @model_validator(mode="before")
    @classmethod
    def _normalize_text_fields(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        cleaned = dict(data)
        for key in ("trigger", "after", "sound"):
            if key in cleaned:
                cleaned[key] = _clean(cleaned[key])
        return cleaned

    @model_validator(mode="after")
    def _require_visual_or_sound(self) -> ReactionBeat:
        if self.visual_id is None and self.sound is None:
            raise ValueError("a reaction beat needs at least one of visual_id or sound")
        return self


class ClosingMedia(BaseModel):
    """The shot to end on: an owned photo held until the end of the clip,
    optionally paired with a sticker badge, starting from the last time
    ``from_trigger`` is spoken (or the last few seconds when unset)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # An owned IMAGE media_id held until the end of the clip.
    visual_id: str = Field(min_length=1, max_length=200)
    # An optional sticker shown alongside it (e.g. a "GOAT" badge).
    badge_visual_id: str | None = Field(default=None, max_length=200)
    # Start from the LAST time this phrase is spoken; None = the last ~3s.
    from_trigger: str | None = Field(default=None, max_length=80)

    @model_validator(mode="before")
    @classmethod
    def _normalize_from_trigger(cls, data: object) -> object:
        if not isinstance(data, dict) or "from_trigger" not in data:
            return data
        cleaned = dict(data)
        cleaned["from_trigger"] = _clean(cleaned["from_trigger"])
        return cleaned


__all__ = ["MAX_REACTION_BEATS", "BeatVisualRole", "ReactionBeat", "ClosingMedia"]
