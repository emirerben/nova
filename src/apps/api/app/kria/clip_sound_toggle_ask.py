"""Did the creator ask to turn the clips' own sound on or off somewhere? (KRI-942)

Don Julio walk-in M1: "Clip sound off except that last line." The brief kept the sentence
(`r7 [audio/global]`) with no facts, so no checker could judge it, and the only machine-readable
copy of the ask was the per-turn strategy's `montage_audio.source_media_ids`, which the next
turn ("add the title") rewrote to `[]`: every clip played its own sound.

The Main Creator (and the narrow brief extractor, which shares its brief section) now records
the meaning on an ``audio`` requirement as typed facts (KRI-667: the model records the meaning,
the server reads the field, no wording is matched here), English keys whatever the language:

- ``clip_sound``: ``"off"`` or ``"on"``, what the ask does. (``under_voice`` / ``after_voice``
  are the voiceover asks of `app.kria.narrated_clip_sound_ask`; the two readers never overlap.)
- where, any combination, all optional (none = the whole video):
  - scope ``clip:<media_id>`` (or ``sound_media``): every cut of that clip;
  - ``sound_clip``: ``"first"`` / ``"last"``, the first or last clip of the finished edit;
  - ``sound_from_s`` / ``sound_to_s``: seconds of the video, or of that clip when the ask names a
    clip (counted from where the clip starts in the edit);
  - ``sound_line``: only where that spoken line is said, the creator's exact words.
- ``sound_except``: parts that get the opposite, each an object with the same "where" keys:
  "Clip sound off except that last line" =>
  ``{"clip_sound": "off", "sound_except": [{"sound_clip": "last", "sound_line": "Super
  excited!"}]}``.

The brief is cumulative, so a turn that doesn't touch the requirement keeps the ask. The phone
montage worker (`app.pipeline.clip_sound_toggle`) builds it and `app.kria.brief_checks` judges
it against the recipe the phone renders.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from app.kria.narrated_clip_sound_ask import CLIP_SOUND_FACT

if TYPE_CHECKING:
    from app.kria.brief import BriefRequirement, CreativeBrief

SOUND_ON = "on"
SOUND_OFF = "off"
SOUND_STATES = frozenset({SOUND_ON, SOUND_OFF})

SOUND_CLIP_FACT = "sound_clip"
SOUND_MEDIA_FACT = "sound_media"
SOUND_FROM_FACT = "sound_from_s"
SOUND_TO_FACT = "sound_to_s"
SOUND_LINE_FACT = "sound_line"
SOUND_EXCEPT_FACT = "sound_except"
CLIP_POSITIONS = frozenset({"first", "last"})

# An ask names at most this many parts that get the opposite, and a part at most this many
# lines: anything longer is not one creator sentence and is left to the words alone.
_MAX_EXCEPTIONS = 6
_MAX_LINES = 3
# Seconds past any phone video's length: a bigger number is not a time in the video.
_MAX_SECONDS = 1800.0

SoundState = Literal["on", "off"]


@dataclass(frozen=True)
class SoundTarget:
    """Where an ask (or one of its exceptions) applies. Nothing set = the whole video."""

    media_ref: str | None = None  # a clip id (or a 6+ letter part of one)
    clip_position: str | None = None  # "first" | "last"
    from_s: float | None = None
    to_s: float | None = None
    lines: tuple[str, ...] = ()
    # KRI-1028: a clip kept audible for a line the creator named must hold that sentence
    # (source seconds, from the approved length plan's ``kept_lines``); never read off the brief.
    kept_line: str | None = None
    kept_span: tuple[float, float] | None = None

    @property
    def names_clip(self) -> bool:
        return self.media_ref is not None or self.clip_position is not None

    @property
    def has_window(self) -> bool:
        return self.from_s is not None or self.to_s is not None

    @property
    def whole_video(self) -> bool:
        return not (self.names_clip or self.has_window or self.lines)


@dataclass(frozen=True)
class SoundToggleAsk:
    """One requirement turning the clips' own sound on or off somewhere."""

    requirement: BriefRequirement
    state: SoundState
    target: SoundTarget
    exceptions: tuple[SoundTarget, ...] = ()

    @property
    def requirement_id(self) -> str:
        return self.requirement.id

    @property
    def targeted(self) -> bool:
        """Whole-video asks set the base; targeted ones are applied after every one of them."""
        return not self.target.whole_video


def _seconds(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and 0 <= number <= _MAX_SECONDS else None


def _lines(value: object) -> tuple[str, ...] | None:
    """The spoken lines a ``sound_line`` names; None when it is malformed."""
    items = value if isinstance(value, (list, tuple)) else [value]
    found: list[str] = []
    for item in items:
        if not isinstance(item, str):
            return None
        line = " ".join(item.split())
        if line and line not in found:
            found.append(line)
    return tuple(found[:_MAX_LINES]) if found else None


def _target(values: Mapping[str, Any], *, media_ref: str | None) -> SoundTarget | None:
    """The "where" keys of ``values``; None when any of them is malformed."""
    position = values.get(SOUND_CLIP_FACT)
    if position is not None:
        position = " ".join(str(position).split()).lower() if isinstance(position, str) else ""
        if position not in CLIP_POSITIONS:
            return None
    media = values.get(SOUND_MEDIA_FACT)
    if media is not None:
        if not isinstance(media, str) or not media.strip():
            return None
        media_ref = media.strip()
    start = end = None
    if values.get(SOUND_FROM_FACT) is not None:
        start = _seconds(values.get(SOUND_FROM_FACT))
        if start is None:
            return None
    if values.get(SOUND_TO_FACT) is not None:
        end = _seconds(values.get(SOUND_TO_FACT))
        if end is None:
            return None
    if start is not None and end is not None and end <= start:
        return None
    lines: tuple[str, ...] = ()
    if values.get(SOUND_LINE_FACT) is not None:
        parsed = _lines(values.get(SOUND_LINE_FACT))
        if parsed is None:
            return None
        lines = parsed
    return SoundTarget(
        media_ref=media_ref, clip_position=position, from_s=start, to_s=end, lines=lines
    )


def requirement_sound_toggle(req: BriefRequirement) -> SoundToggleAsk | None:
    """``req`` as an on/off ask when its recorded facts say exactly that, else None."""
    if not req.live:
        return None
    value = req.facts.get(CLIP_SOUND_FACT)
    state = " ".join(value.split()).lower() if isinstance(value, str) else ""
    if state not in SOUND_STATES:
        return None
    scoped = req.scope.split(":", 1)[1].strip() if req.scope.startswith("clip:") else None
    target = _target(req.facts, media_ref=scoped or None)
    if target is None:
        return None
    raw = req.facts.get(SOUND_EXCEPT_FACT)
    exceptions: list[SoundTarget] = []
    if raw is not None:
        if not isinstance(raw, list) or len(raw) > _MAX_EXCEPTIONS:
            return None
        for part in raw:
            parsed = _target(part, media_ref=None) if isinstance(part, Mapping) else None
            if parsed is None or parsed.whole_video:
                # An exception that covers everything is no exception: the ask is unclear.
                return None
            exceptions.append(parsed)
    return SoundToggleAsk(
        requirement=req,
        state=state,  # type: ignore[arg-type]
        target=target,
        exceptions=tuple(exceptions),
    )


def sound_toggle_asks(requirements: Iterable[BriefRequirement]) -> tuple[SoundToggleAsk, ...]:
    """Every on/off ask among ``requirements``, in brief order."""
    return tuple(ask for req in requirements if (ask := requirement_sound_toggle(req)) is not None)


def requirement_source_mute(req: BriefRequirement) -> SoundToggleAsk | None:
    """KRI-1297: a global ``audio_intent: source_muted`` ask read as a whole-video ``off``.

    "Mute my sound, I'll add a song on TikTok" is filed by the brief writer as the typed
    ``source_muted`` (the voiceover bed's mute, KRI-821) rather than ``clip_sound: off``. One
    plain ask, two typed shapes: without this a voiceless montage kept the clip's own sound.
    The approved words must say mute/remove and never keep, lower or raise the sound; a
    clip-scoped or windowed ask stays the bed reader's. A voiceover or song soundtrack never
    reaches this (the montage lane steps aside for them), so the bed mute is unchanged.
    """
    from app.kria.brief_checks import (  # noqa: PLC0415
        _BED_MUTE_RE,
        _BED_UP_RE,
        _KEEP_VERB_RE,
        SOURCE_MUTED_INTENT,
        _asks_bed_level,
        _req_text,
        _typed_audio_intent,
    )

    if not req.live or req.kind != "audio" or req.scope.strip().lower() != "global":
        return None
    if _typed_audio_intent(req) != SOURCE_MUTED_INTENT:
        return None
    if req.facts.get(CLIP_SOUND_FACT) is not None:
        return None
    text = _req_text(req)
    if (
        not _BED_MUTE_RE.search(text)
        or _KEEP_VERB_RE.search(text)
        or _BED_UP_RE.search(text)
        or _asks_bed_level(text)
    ):
        return None
    return SoundToggleAsk(requirement=req, state=SOUND_OFF, target=SoundTarget())


def brief_sound_toggle_asks(brief: CreativeBrief | None) -> tuple[SoundToggleAsk, ...]:
    """`sound_toggle_asks` over a brief's live requirements (empty without one).

    Includes the typed ``source_muted`` whole-video mute (`requirement_source_mute`).
    """
    if brief is None:
        return ()
    asks = list(sound_toggle_asks(brief.live()))
    seen = {ask.requirement_id for ask in asks}
    asks.extend(
        ask
        for req in brief.live()
        if req.id not in seen and (ask := requirement_source_mute(req)) is not None
    )
    return tuple(asks)


def mutes_whole_video(asks: Iterable[SoundToggleAsk]) -> bool:
    """True when the asks silence every clip (Qendresa case 4, "mute my sound"): no ask turns
    any sound on (no ``on`` ask, no exception on any ask: each exception is painted on, in
    brief order) and at least one is a whole-video ``off``."""
    asks = tuple(asks)
    if any(ask.state == SOUND_ON or ask.exceptions for ask in asks):
        return False
    return any(ask.state == SOUND_OFF and ask.target.whole_video for ask in asks)


def brief_mutes_whole_video(brief: CreativeBrief | None) -> bool:
    """`mutes_whole_video` over a brief's live requirements (False without one)."""
    return mutes_whole_video(brief_sound_toggle_asks(brief))


__all__ = [
    "CLIP_POSITIONS",
    "SOUND_CLIP_FACT",
    "SOUND_EXCEPT_FACT",
    "SOUND_FROM_FACT",
    "SOUND_LINE_FACT",
    "SOUND_MEDIA_FACT",
    "SOUND_OFF",
    "SOUND_ON",
    "SOUND_STATES",
    "SOUND_TO_FACT",
    "SoundTarget",
    "SoundToggleAsk",
    "brief_mutes_whole_video",
    "brief_sound_toggle_asks",
    "mutes_whole_video",
    "requirement_source_mute",
    "requirement_sound_toggle",
    "sound_toggle_asks",
]
