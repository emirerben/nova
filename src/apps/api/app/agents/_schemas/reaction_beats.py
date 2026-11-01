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

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

MAX_REACTION_BEATS = 24

BeatVisualRole = Literal["photo", "sticker"]
# Mirrors `app.kria.portable_speaker_background.SpeakerFramePreset` (importing it here would be
# circular through `app.kria`); `test_speaker_frame_presets_mirror_the_portable_contract` pins it.
SpeakerFramePreset = Literal[
    "full", "small_bottom_right", "small_bottom_left", "small_top_right", "small_top_left"
]


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
    # An owned IMAGE media_id from the manifest (photo or sticker), or a
    # Visuals VIDEO where the phone draws video cards (KRI-521). Resolved
    # (label/media_id match, unknown -> dropped) by
    # `app.services.creator_capabilities.compile_strategy_to_plan`.
    visual_id: str | None = Field(default=None, max_length=200)
    visual_role: BeatVisualRole = "sticker"
    # Voiceover edits can make a requested visual a full-frame cutaway.  The
    # legacy/default card remains picture-in-picture, and the serializer omits
    # both new fields at their defaults so existing stored strategies retain
    # their exact wire shape.
    display_mode: Literal["pip", "fullscreen"] = "pip"
    # A full-screen voiceover beat can hand back to one selected original
    # video after the visual.  This is a manifest media id, repaired by the
    # capability layer; it must never identify a Visuals-pool video.
    following_clip_id: str | None = Field(default=None, max_length=200)
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

    @model_serializer(mode="wrap")
    def _omit_default_voiceover_fields(self, handler):  # noqa: ANN001, ANN202
        """Keep pre-N2 reaction-beat payloads byte-for-byte compatible."""

        data = handler(self)
        if self.display_mode == "pip":
            data.pop("display_mode", None)
        if self.following_clip_id is None:
            data.pop("following_clip_id", None)
        return data


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


class EndingClip(BaseModel):
    """A video the creator asked to append AFTER the talk, playing its own sound (KRI-841).

    "At the very end, add the cheers video with its own sound": unlike ``ClosingMedia`` (a
    photo held over the last seconds of the talk), the talk plays to its end and this video
    follows on the main track, so the phone's own audio for it is the clip's sound. ``media_id``
    is an opaque manifest id: a Visuals video (``asset-*``) or one of the item's own footage
    clips (multi-clip Talking). The server repair
    (``app.services.creator_capabilities.repair_creator_ending_clip``) keeps it only where the
    phone can render it and canonicalizes the id; the worker records what it really appended
    (``variant["phone_ending_clip"]``).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    media_id: str = Field(min_length=1, max_length=200)
    # False only when the creator asked for the video silent.
    keep_audio: bool = True


MAX_BACKGROUND_SEQUENCE = 40
DEFAULT_BACKGROUND_CADENCE_S = 4.0


class BackgroundSequenceEntry(BaseModel):
    """One photo / muted looping video of a commentary-style background sequence (KRI-1162).

    ``media_id`` is a Visuals-pool manifest id. ``spoken_phrase`` (the creator's words, in
    their language) anchors the entry to the first time that phrase is heard in the kept
    take ("whichever photo I'm talking about"); an entry without one is laid out by cadence.
    ``hold_s`` overrides how long the entry stays up. The worker resolves all of it into
    timed ``windows`` (`generative_build._resolve_strategy_background`); this is the REQUEST.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    media_id: str = Field(min_length=1, max_length=200)
    # Video only: where the looping background starts playing (a photo ignores it).
    source_start_s: float = Field(default=0.0, ge=0.0, le=1800.0, allow_inf_nan=False)
    spoken_phrase: str | None = Field(default=None, max_length=120)
    hold_s: float | None = Field(default=None, ge=0.5, le=30.0, allow_inf_nan=False)

    @model_validator(mode="before")
    @classmethod
    def _normalize_phrase(cls, data: object) -> object:
        if not isinstance(data, dict) or "spoken_phrase" not in data:
            return data
        cleaned = dict(data)
        cleaned["spoken_phrase"] = _clean(cleaned["spoken_phrase"])
        return cleaned

    @model_serializer(mode="wrap")
    def _omit_defaults(self, handler):  # noqa: ANN001, ANN202
        data = handler(self)
        if data.get("source_start_s") == 0.0:
            data.pop("source_start_s", None)
        if data.get("spoken_phrase") is None:
            data.pop("spoken_phrase", None)
        if data.get("hold_s") is None:
            data.pop("hold_s", None)
        return data


class BackgroundMedia(BaseModel):
    """The photo or looping video shown BEHIND the speaker (KRI-1117, KRI-1162).

    "Put this photo behind me" / the creator's pick in the camera: a green-screen style
    background on a phone Talking edit. ``media_id`` is an opaque manifest media id of a
    Visuals-pool photo or video (``asset-*``), never one of the speaker's own clips. The
    server repair (``app.services.creator_capabilities.repair_creator_speaker_background``)
    keeps it only where the phone can composite it and canonicalizes the id; the worker
    resolves it into the lane request's ``background``.

    KRI-1162 (commentary-style green screen), all omitted from the dump while unused:
    ``sequence`` is an ordered list of pool photos / videos that take turns behind the
    speaker (``media_id`` stays the FILL shown wherever no window covers the time),
    ``cadence_s`` how long an unanchored entry stays up, ``speaker_frame`` a NAMED corner for
    the matted speaker (never geometry from the model, S37).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    media_id: str = Field(min_length=1, max_length=200)
    # Video only: where the looping background starts playing (a photo ignores it).
    source_start_s: float = Field(default=0.0, ge=0.0, le=1800.0, allow_inf_nan=False)
    sequence: tuple[BackgroundSequenceEntry, ...] = Field(
        default=(), max_length=MAX_BACKGROUND_SEQUENCE
    )
    cadence_s: float = Field(
        default=DEFAULT_BACKGROUND_CADENCE_S, ge=2.0, le=10.0, allow_inf_nan=False
    )
    speaker_frame: SpeakerFramePreset = "full"

    @model_serializer(mode="wrap")
    def _omit_default_start(self, handler):  # noqa: ANN001, ANN202
        data = handler(self)
        if data.get("source_start_s") == 0.0:
            data.pop("source_start_s", None)
        if not data.get("sequence"):
            data.pop("sequence", None)
        if data.get("cadence_s") == DEFAULT_BACKGROUND_CADENCE_S:
            data.pop("cadence_s", None)
        if data.get("speaker_frame") == "full":
            data.pop("speaker_frame", None)
        return data


MAX_TIMED_CARDS = 12


class TimedCard(BaseModel):
    """A photo/video shown as a SMALL picture-in-picture card at an exact moment of a phone
    Montage or Voiceover edit ("show the menu photo as a small card at 3 seconds for 2
    seconds", "put the receipt in the corner during the second clip").

    Unlike ``ReactionBeat`` this needs no spoken word: the anchor is either ``at_s`` on the
    OUTPUT timeline, or ``clip_id`` + ``offset_s`` (seconds after that clip starts in the
    finished video; the worker resolves the clip's real start on the compiled timeline, after
    transitions and refits). Exactly one of the two anchors is set. ``visual_id`` is an opaque
    manifest media id (a Visuals photo, or a Visuals video where the device draws video cards),
    canonicalised by ``app.services.creator_capabilities.repair_creator_timed_cards``; ``label``
    is the creator's own words for it, kept for the receipt only. The worker grounds each card
    (`app.pipeline.phone_timed_cards.ground_timed_cards`) into a ``SubtitledOverlayCard``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    card_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,40}$")
    visual_id: str = Field(min_length=1, max_length=200)
    label: str | None = Field(default=None, max_length=80)
    at_s: float | None = Field(default=None, ge=0.0, le=1800.0)
    clip_id: str | None = Field(default=None, min_length=1, max_length=200)
    offset_s: float = Field(default=0.0, ge=0.0, le=1800.0)
    hold_s: float = Field(default=2.0, ge=0.5, le=8.0)

    @model_validator(mode="before")
    @classmethod
    def _normalize_label(cls, data: object) -> object:
        if not isinstance(data, dict) or "label" not in data:
            return data
        cleaned = dict(data)
        cleaned["label"] = _clean(cleaned["label"])
        return cleaned

    @model_validator(mode="after")
    def _one_anchor(self) -> TimedCard:
        if (self.at_s is None) == (self.clip_id is None):
            raise ValueError("a timed card needs exactly one anchor: at_s or clip_id")
        return self

    @model_serializer(mode="wrap")
    def _omit_defaults(self, handler):  # noqa: ANN001, ANN202
        data = handler(self)
        if self.label is None:
            data.pop("label", None)
        if self.at_s is None:
            data.pop("at_s", None)
        if self.clip_id is None:
            data.pop("clip_id", None)
            data.pop("offset_s", None)
        return data


__all__ = [
    "MAX_REACTION_BEATS",
    "MAX_BACKGROUND_SEQUENCE",
    "MAX_TIMED_CARDS",
    "BeatVisualRole",
    "ReactionBeat",
    "ClosingMedia",
    "EndingClip",
    "BackgroundMedia",
    "BackgroundSequenceEntry",
    "TimedCard",
]
