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

import math
import re
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_serializer,
    model_validator,
)

from app.text_fold import loose_text

MAX_REACTION_BEATS = 24

BeatVisualRole = Literal["photo", "sticker"]
# Mirrors `app.kria.portable_speaker_background.SpeakerFramePreset` (importing it here would be
# circular through `app.kria`); `test_speaker_frame_presets_mirror_the_portable_contract` pins it.
SpeakerFramePreset = Literal[
    "full", "small_bottom_right", "small_bottom_left", "small_top_right", "small_top_left"
]


# KRI-1173: the colours a creator may NAME for a plain background (EN and TR), and the one
# spelling (`#RRGGBB` uppercase) the Save, the lane and the recipe agree on. Shared by the chat
# strategy repair and the copilot op so the two never disagree on what "green" is.
BACKGROUND_COLOR_PRESETS: dict[str, str] = {
    "white": "#FFFFFF",
    "black": "#000000",
    "green": "#00FF00",
    "blue": "#0000FF",
    "red": "#FF0000",
    "yellow": "#FFFF00",
    "grey": "#808080",
    "gray": "#808080",
    "beyaz": "#FFFFFF",
    "siyah": "#000000",
    "yeşil": "#00FF00",
    "mavi": "#0000FF",
    "kırmızı": "#FF0000",
    "sarı": "#FFFF00",
    "gri": "#808080",
}
_BACKGROUND_HEX = re.compile(r"^#?([0-9A-Fa-f]{6})$")
# The presets keyed by the shared Turkish-aware loose fold (KRI-786: no private fold tables), so
# "KIRMIZI", "Kırmızı" and ASCII-typed "kirmizi" all name red.
_PRESET_BY_FOLD: dict[str, str] = {loose_text(k): v for k, v in BACKGROUND_COLOR_PRESETS.items()}


def normalise_background_color(value: object) -> str | None:
    """``#RRGGBB`` uppercase for a hex (any case, ``#`` optional) or a named preset, else None.

    Short ``#RGB``, alpha hex and any other colour name are refused rather than guessed.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    match = _BACKGROUND_HEX.fullmatch(text)
    if match is not None:
        return "#" + match.group(1).upper()
    return _PRESET_BY_FOLD.get(loose_text(text))


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
    # "title": the visual is shown with the opening title, right above it, not
    # on a spoken word; the trigger names the visual and is never listened for.
    # Omitted from the wire shape at its default, like the fields above.
    anchor: Literal["word", "title"] = "word"

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
        if self.anchor == "word":
            data.pop("anchor", None)
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


# The size the recipe compiler and the sentence check are built for (the same number as
# `phone_subtitled_plan.MAX_INSERT_CLIPS`, pinned equal by a test): a creator who asks for more is
# told the number, not silently cut.
MAX_INSERT_CLIPS = 20
# Rows kept when parsing, so the repair can name each one past the cap (`too_many`) instead of
# the parse cutting it unseen.
MAX_INSERT_CLIP_ROWS = MAX_INSERT_CLIPS + 10


class InsertClip(BaseModel):
    """A clip of the creator's own footage that plays while his take is PAUSED (parent KRI-1151).

    "After I say 'congress', show the congress clip with its sound": the picture switches to the
    clip and its audio plays, then the take resumes where it stopped, so the video grows by the
    clip's length. Unlike ``EndingClip`` (after the whole talk) and a cutaway (the take keeps
    running under it), nothing the creator said is lost. ``media_id`` is an opaque manifest
    media id of one of the item's own footage clips; the server repair
    (``app.services.creator_capabilities.repair_creator_insert_clips``) keeps it only where the
    phone can render it and canonicalizes the id. At most one of ``at_phrase`` (words the
    creator says; the clip plays at the next natural pause after them) or ``at_s`` (a second of
    the take) names the moment. With neither ("place them in between my speech") the clip plays at
    the end of the sentence the creator says about it, chosen by meaning by the worker
    (`app.pipeline.phone_talking_insert_topics`); the worker records what it really placed
    (``variant["phone_insert_clips"]``). Carries no resolved timestamp.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    media_id: str = Field(min_length=1, max_length=200)
    at_phrase: str | None = Field(default=None, max_length=120)
    at_s: float | None = Field(default=None, ge=0.0, le=1800.0, allow_inf_nan=False)
    # False when the creator asked for the clip silent or it has no sound of its own.
    keep_audio: bool = True

    @model_validator(mode="before")
    @classmethod
    def _normalize_phrase(cls, data: object) -> object:
        if not isinstance(data, dict) or "at_phrase" not in data:
            return data
        cleaned = dict(data)
        cleaned["at_phrase"] = _clean(cleaned["at_phrase"])
        return cleaned

    @model_validator(mode="after")
    def _one_position(self) -> InsertClip:
        if self.at_phrase is not None and self.at_s is not None:
            raise ValueError("an insert clip takes at most one of at_phrase or at_s")
        return self

    @model_serializer(mode="wrap")
    def _omit_unset_position(self, handler):  # noqa: ANN001, ANN202
        data = handler(self)
        for key in ("at_phrase", "at_s"):
            if data.get(key) is None:
                data.pop(key, None)
        return data


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


MAX_BACKGROUND_LEFT_OUT = 8


class BackgroundLeftOut(BaseModel):
    """One repo / recording the planner left out of a background sequence, and why (KRI-1304).

    ``why``: "no_recording" (the creator talks about it but no photo / video for it is attached,
    so the previous one stays on) or "not_said" (a recording exists but the take never mentions
    it). The worker carries it onto the lane receipt so the finished-video reply names it; the
    planner's private ``summary`` never reaches the creator after a render.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=60)
    why: Literal["no_recording", "not_said"]


def _left_out_rows(value: object) -> tuple[dict[str, str], ...]:
    """Valid ``{name, why}`` rows from model output; junk is dropped, never an error."""
    rows: list[dict[str, str]] = []
    for item in value if isinstance(value, (list, tuple)) else ():
        raw = item.model_dump() if isinstance(item, BackgroundLeftOut) else item
        if not isinstance(raw, dict):
            continue
        name = _clean(raw.get("name"))
        why = raw.get("why")
        if name and why in ("no_recording", "not_said"):
            rows.append({"name": name[:60], "why": why})
    return tuple(rows[:MAX_BACKGROUND_LEFT_OUT])


def _opening_seconds(value: object) -> float | None:
    """A real number (never a bool or text) from 1 to 15 seconds, else ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if 1.0 <= number <= 15.0 else None


_FOCUS_WORDS = {
    "left": "left",
    "center": "center",
    "centre": "center",
    "middle": "center",
    "right": "right",
}
MIN_FOCUS_ZOOM_ASKED = 0.3
MAX_FOCUS_ZOOM_ASKED = 3.0


def _focus_word(value: object) -> str | None:
    """``left`` / ``center`` / ``right`` (any case; ``centre`` / ``middle`` too), else None."""
    return _FOCUS_WORDS.get(value.strip().casefold()) if isinstance(value, str) else None


def _focus_zoom(value: object) -> float | None:
    """A real, finite number clamped to 0.3..3.0 (never a bool or text), else ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return min(MAX_FOCUS_ZOOM_ASKED, max(MIN_FOCUS_ZOOM_ASKED, number))


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

    KRI-1173 (a plain colour): ``color`` replaces ``media_id`` -- EXACTLY ONE of the two is
    set. It is the creator's colour as ``#RRGGBB`` (the repair also accepts the named presets
    and any case and rewrites it to uppercase hex; anything else is dropped with a notice, so
    the field is a loose string here and a bad value never fails the turn's schema).
    ``source_start_s`` belongs to a media fill only; ``sequence`` / ``speaker_frame`` keep their
    meaning (the windows play over the colour). Omitted from the dump while unused.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    media_id: str | None = Field(default=None, min_length=1, max_length=200)
    color: str | None = Field(default=None, min_length=1, max_length=32)
    # Video only: where the looping background starts playing (a photo ignores it).
    source_start_s: float = Field(default=0.0, ge=0.0, le=1800.0, allow_inf_nan=False)
    sequence: tuple[BackgroundSequenceEntry, ...] = Field(
        default=(), max_length=MAX_BACKGROUND_SEQUENCE
    )
    cadence_s: float = Field(
        default=DEFAULT_BACKGROUND_CADENCE_S, ge=2.0, le=10.0, allow_inf_nan=False
    )
    speaker_frame: SpeakerFramePreset = "full"
    # Qendresa item 16: the speaker's own take full screen before the background / corner starts.
    # Both are omitted from the dump while unused and lenient on junk (a bad model value never
    # fails the turn).
    full_screen_opening: bool = False
    full_screen_opening_s: float | None = None
    # Qendresa item 18: which side of the recordings behind the speaker to show, and how close.
    # Omitted from the dump while unused; lenient on junk (a bad model value never fails the turn).
    focus: Literal["left", "center", "right"] | None = None
    focus_zoom: float | None = None
    # KRI-1304: what the planner left out of the sequence and why, so the finished-video reply can
    # say it. Omitted from the dump while empty; lenient on junk (a bad model value never fails
    # the turn).
    left_out: tuple[BackgroundLeftOut, ...] = ()

    @field_validator("left_out", mode="before")
    @classmethod
    def _lenient_left_out(cls, value: object) -> tuple[BackgroundLeftOut, ...]:
        return tuple(BackgroundLeftOut(**row) for row in _left_out_rows(value))

    @field_validator("focus", mode="before")
    @classmethod
    def _lenient_focus(cls, value: object) -> str | None:
        return _focus_word(value)

    @field_validator("focus_zoom", mode="before")
    @classmethod
    def _lenient_focus_zoom(cls, value: object) -> float | None:
        return _focus_zoom(value)

    @model_validator(mode="before")
    @classmethod
    def _opening_length_implies_the_opening(cls, data: object) -> object:
        if isinstance(data, dict) and data.get("full_screen_opening") is None:
            if _opening_seconds(data.get("full_screen_opening_s")) is not None:
                return {**data, "full_screen_opening": True}
        return data

    @field_validator("full_screen_opening", mode="before")
    @classmethod
    def _lenient_full_screen_opening(cls, value: object) -> bool:
        try:
            return TypeAdapter(bool).validate_python(value)
        except ValueError:
            return False

    @field_validator("full_screen_opening_s", mode="before")
    @classmethod
    def _lenient_full_screen_opening_s(cls, value: object) -> float | None:
        return _opening_seconds(value)

    @model_validator(mode="after")
    def _one_fill(self) -> BackgroundMedia:
        if (self.media_id is None) == (self.color is None):
            raise ValueError("a background needs exactly one of media_id or color")
        if self.color is not None and self.source_start_s != 0.0:
            raise ValueError("a colour background has no source start")
        return self

    @model_serializer(mode="wrap")
    def _omit_default_start(self, handler):  # noqa: ANN001, ANN202
        data = handler(self)
        if data.get("media_id") is None:
            data.pop("media_id", None)
        if data.get("color") is None:
            data.pop("color", None)
        if data.get("source_start_s") == 0.0:
            data.pop("source_start_s", None)
        if not data.get("sequence"):
            data.pop("sequence", None)
        if data.get("cadence_s") == DEFAULT_BACKGROUND_CADENCE_S:
            data.pop("cadence_s", None)
        if data.get("speaker_frame") == "full":
            data.pop("speaker_frame", None)
        if data.get("full_screen_opening") is False:
            data.pop("full_screen_opening", None)
        if data.get("full_screen_opening_s") is None:
            data.pop("full_screen_opening_s", None)
        if data.get("focus") is None:
            data.pop("focus", None)
        if data.get("focus_zoom") is None:
            data.pop("focus_zoom", None)
        if not data.get("left_out"):
            data.pop("left_out", None)
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
    "BACKGROUND_COLOR_PRESETS",
    "normalise_background_color",
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
