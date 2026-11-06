"""Did the creator ask the footage to play faster or slower, and how? (KRI-689)

"Speed the river shots up 2x" lands in the Creative Brief as a ``style`` requirement. The phone
montage planner (`pipeline.unified_montage`) now reads such an ask into a `SpeedSpec` -- the
rate, the creator's words for the footage (or a cut position, or a ``clip:<id>`` scope) -- and
writes ``playback_rate`` onto the matching cuts; the phone recipe plays them at that rate
(`phone_guided_plan`, ``variableSpeed``). `brief_checks` turns the plan's record into the
receipt ("Clip 2 plays at 2x", or why not) and keeps an honest refusal only where no plan can
carry the speed (no verified renderer, a song montage, a lip-synced take, a frozen frame, a
photo, a speed ramp).

Evidence, in order (KRI-667): a recorded fact decides. ``playback_rate`` (or ``speed``) is a
number, 2 = twice as fast, 0.5 = half speed, 1 = no change; the Main Creator is asked for it
(``main_creator._BRIEF_PROMPT_SECTION``). Without it the creator's words are read, deliberately
narrowly and with no ``re.*`` (the KRI-667 ratchet): a phrase that can only mean retiming, or a
speed verb aimed at the footage, never pacing, text or music ("fast pacing", "speed up the
title animation"). The words also give the number ("2x", "2 kat", "half speed"); "slow motion"
with no number is `DEFAULT_SLOW_RATE`, "speed up" / "time-lapse" with no number is
`DEFAULT_FAST_RATE`, and the receipt says the creator named none.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.kria.brief_route import loose_text

if TYPE_CHECKING:
    from app.kria.brief import BriefRequirement

# The model files "speed the river shots up" under style; a pacing-flavoured wording lands in
# timing or select. Text, order and audio asks have their own checkers.
SPEED_ASK_KINDS = frozenset({"style", "timing", "select"})
RATE_FACT_KEYS = ("playback_rate", "speed")

# The native range (`GuidedStoryMoment.playback_rate`, `phone_guided_plan`): a rate outside it
# is held at the bound and the receipt says so.
MIN_PLAYBACK_RATE = 0.25
MAX_PLAYBACK_RATE = 4.0
# "speed up" / "slow motion" with no number.
DEFAULT_FAST_RATE = 2.0
DEFAULT_SLOW_RATE = 0.5

# Adjacent tokens (diacritics folded) that can only mean retiming footage.
_RETIME_PHRASES: tuple[tuple[str, ...], ...] = (
    ("slow", "motion"),
    ("slow", "mo"),
    ("slo", "mo"),
    ("slowmo",),
    ("slomo",),
    ("timelapse",),
    ("time", "lapse"),
    ("hyperlapse",),
    ("hyper", "lapse"),
    ("fast", "motion"),
    ("fast", "forward"),
    ("playback", "speed"),
    ("playback", "rate"),
    ("double", "speed"),
    ("half", "speed"),
    ("quarter", "speed"),
    ("speed", "ramp"),
    ("speed", "ramps"),
    ("speed", "ramping"),
    # Turkish: ağır çekim, yavaş çekim, zaman atlamalı
    ("agir", "cekim"),
    ("yavas", "cekim"),
    ("zaman", "atlamali"),
)
_SPEED_VERBS = frozenset({"speed", "sped", "speeding", "slow", "slowed", "slowing"})
_PARTICLES = frozenset({"up", "down"})
_VERB_PARTICLE_GAP = 4  # "speed the river shots up": up to four words between them
_SPEED_WORDS = frozenset({"speed", "faster", "slower", "quicker", "fast", "slow"})
_FOOTAGE_NOUNS = frozenset(
    ("clip", "clips", "shot", "shots", "footage", "video", "videos", "take", "takes")
    + ("scene", "scenes")
)
# What the verbs also describe without touching the footage: the rhythm of the edit, the text
# on it, the sound under it. Any of these in the same ask means "not clip speed".
_NOT_FOOTAGE = frozenset(
    ("cut", "cuts", "cutting", "pacing", "pace", "paced", "transition", "transitions", "tempo")
    + ("rhythm", "edit", "editing", "text", "title", "titles", "caption", "captions")
    + ("subtitle", "subtitles", "label", "labels", "animation", "animations", "typing")
    + ("music", "song", "audio", "sound")
    # A size or a zoom ("make the clip 2x bigger", "zoom in 2x") is not a speed.
    + ("bigger", "larger", "smaller", "zoom", "zoomed", "crop", "cropped")
)
# Turkish stems (hızlandır, yavaşlat) and the footage / non-footage words around them.
_TR_STEMS = ("hizlandir", "yavaslat")
_TR_FOOTAGE = ("klip", "klib", "cekim", "video", "goruntu", "sahne", "kayit")
_TR_NOT_FOOTAGE = (
    "kesim",
    "gecis",
    "tempo",
    "montaj",
    "yazi",
    "baslik",
    "altyazi",
    "animasyon",
) + ("etiket", "muzik", "sarki")
_TR_SPEEDY = ("hizli", "hizlan", "yavas")

# -- how fast ---------------------------------------------------------------------------
# Words that say the footage should play SLOWER / FASTER (no number needed).
_SLOW_WORDS = frozenset({"slow", "slower", "slowed", "slowing", "slowmo", "slomo", "slo"})
_FAST_WORDS = frozenset(
    {"speed", "sped", "speeding", "faster", "quicker", "fast", "timelapse", "hyperlapse"}
    | {"lapse", "forward"}
)
_TR_SLOW_STEMS = ("yavas", "agir")
_TR_FAST_STEMS = ("hizl", "hizlan", "zaman")
# A multiplier the words name: "double", "iki kat" ... as a number of times.
_TIMES_WORDS = {
    "double": 2.0,
    "twice": 2.0,
    "triple": 3.0,
    "two": 2.0,
    "three": 3.0,
    "four": 4.0,
    "iki": 2.0,
    "uc": 3.0,
    "dort": 4.0,
}
# A fraction of normal speed: never inverted, whatever the direction words say.
_FRACTION_WORDS = {"half": 0.5, "quarter": 0.25, "yari": 0.5, "yarim": 0.5, "ceyrek": 0.25}
_TIMES_MARKERS = frozenset({"times", "kat", "x"})
_RAMP_WORDS = frozenset({"ramp", "ramps", "ramping"})

# -- which footage ----------------------------------------------------------------------
# Words of the ask itself, never of the footage it names.
_ASK_WORDS = frozenset(
    _SPEED_VERBS
    | _PARTICLES
    | _SPEED_WORDS
    | _SLOW_WORDS
    | _FAST_WORDS
    | _RAMP_WORDS
    | _TIMES_MARKERS
    | set(_TIMES_WORDS)
    | set(_FRACTION_WORDS)
    | _FOOTAGE_NOUNS
    | {"motion", "mo", "time", "playback", "rate", "speeds", "speedy", "ramped"}
    | {"part", "parts", "bit", "bits", "section", "sections", "moment", "moments", "one", "ones"}
    | {"the", "a", "an", "it", "them", "all", "every", "everything", "whole", "entire", "each"}
    | {"on", "for", "of", "in", "to", "at", "with", "through", "and", "or", "so", "that", "this"}
    | {"these", "those", "my", "our", "his", "her", "their", "its", "there", "here", "where"}
    | {"please", "make", "put", "add", "do", "let", "lets", "just", "only", "little", "lot"}
    | {"way", "more", "less", "much", "very", "really", "then", "now", "also", "too", "some"}
    | {"play", "plays", "playing", "run", "runs", "go", "goes", "want", "like", "would", "can"}
    | {"you", "me", "we", "i", "s", "is", "are", "be", "as", "into", "from", "by", "up", "down"}
    # Turkish ask words: the footage nouns, the verbs, "kat", "olsun", "yap", pronouns.
    | {"klip", "klibi", "klibini", "klipler", "klipleri", "cekim", "cekimi", "cekimleri"}
    | {"cekimlerini", "video", "videoyu", "videolari", "goruntu", "goruntuyu", "goruntuleri"}
    | {"goruntulerini", "sahne", "sahneyi", "sahneleri", "kayit", "kaydi", "kat", "olsun"}
    | {"yap", "yapsin", "istiyorum", "isterim", "oynat", "oynasin", "oynatsin", "biraz", "daha"}
    | {"cok", "tum", "butun", "hepsi", "hepsini", "her", "ve", "ile", "icin", "bir", "bu", "su"}
    | {"o", "onu", "bunu", "sunu", "lutfen", "gibi", "kadar", "hiz", "hizi", "hizini", "hizda"}
    | {"atlamali", "zaman", "agir", "yavas", "hizli", "hizlandir", "yavaslat", "hizlandirin"}
)


def _tokens(text: str) -> list[str]:
    folded = loose_text(text).replace("×", "x")
    return "".join(ch if ch.isalnum() else " " for ch in folded).split()


def _number_tokens(text: str) -> list[str]:
    """`_tokens`, but "1.5x" / "1,5x" / "0.5" keep their decimal (one number, one token)."""
    folded = loose_text(text).replace("×", "x")
    chars = []
    for index, ch in enumerate(folded):
        decimal_mark = (
            ch in ".,"
            and 0 < index < len(folded) - 1
            and folded[index - 1].isdigit()
            and folded[index + 1].isdigit()
        )
        chars.append("." if decimal_mark else ch if ch.isalnum() else " ")
    return "".join(chars).split()


def _as_number(token: str) -> float | None:
    head, dot, tail = token.partition(".")
    if head.isdigit() and (not dot or tail.isdigit()):
        return float(token)
    return None


def _is_multiplier(token: str) -> bool:
    """ "2x" or "x2": a number of times."""
    if token.endswith("x") and token[:-1].isdigit():
        return True
    return token.startswith("x") and token[1:].isdigit()


def _multiplier_value(token: str) -> float | None:
    """ "2x" / "x2" / "1.5x" -> the number, else None."""
    if token.endswith("x") and len(token) > 1:
        return _as_number(token[:-1])
    if token.startswith("x") and len(token) > 1:
        return _as_number(token[1:])
    return None


def _has_phrase(tokens: Sequence[str]) -> bool:
    for phrase in _RETIME_PHRASES:
        width = len(phrase)
        if any(tuple(tokens[i : i + width]) == phrase for i in range(len(tokens) - width + 1)):
            return True
    return False


def _verb_with_particle(tokens: Sequence[str]) -> bool:
    for index, token in enumerate(tokens):
        if token in _SPEED_VERBS and _PARTICLES.intersection(
            tokens[index + 1 : index + 2 + _VERB_PARTICLE_GAP]
        ):
            return True
    return False


def _english_words(tokens: Sequence[str]) -> bool:
    if _has_phrase(tokens):
        return True
    if _NOT_FOOTAGE.intersection(tokens):
        return False
    multiplier = any(_is_multiplier(token) for token in tokens)
    if multiplier and _SPEED_WORDS.intersection(tokens):
        return True  # "2x speed", "2x faster"
    if multiplier and _FOOTAGE_NOUNS.intersection(tokens):
        return True  # "make the walking clip 2x" (a zoom or a size says "bigger" / "zoom")
    if "times" in tokens and _SPEED_WORDS.intersection(tokens):
        return True  # "three times faster"
    return _verb_with_particle(tokens) and (multiplier or bool(_FOOTAGE_NOUNS.intersection(tokens)))


def _turkish_words(tokens: Sequence[str]) -> bool:
    if any(token.startswith(_TR_NOT_FOOTAGE) for token in tokens):
        return False
    speedy = any(token.startswith(_TR_SPEEDY) for token in tokens)
    if speedy and ("kat" in tokens or any(_is_multiplier(token) for token in tokens)):
        return True  # "2 kat hızlı", "iki kat hızlandır", "1,5x hızlı oynat"
    return any(token.startswith(_TR_STEMS) for token in tokens) and (
        any(token.startswith(_TR_FOOTAGE) for token in tokens)
        or any(_is_multiplier(token) for token in tokens)
    )


def _recorded_rate(req: BriefRequirement) -> float | None:
    for key in RATE_FACT_KEYS:
        value = req.facts.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if math.isfinite(value) and value > 0:
            return float(value)
    return None


def asks_clip_speed(req: BriefRequirement) -> bool:
    """True when ``req`` asks some of the footage to play faster or slower."""
    if req.kind not in SPEED_ASK_KINDS or req.scope == "title":
        return False
    rate = _recorded_rate(req)
    if rate is not None:
        return rate != 1.0
    return says_clip_speed(req.description or "")


def says_clip_speed(text: str) -> bool:
    """True when the words alone ask footage to play faster or slower (`asks_clip_speed`'s
    reading, for words that are not yet a requirement, e.g. a clip-intent quote)."""
    tokens = _tokens(text)
    return _english_words(tokens) or _turkish_words(tokens)


@dataclass(frozen=True)
class SpeedSpec:
    """A creator's clip-speed ask, read from the approved requirement (KRI-689).

    ``rate`` is source seconds per output second, held inside the native range
    (`MIN_PLAYBACK_RATE`..`MAX_PLAYBACK_RATE`); ``rate_requested`` is the number as asked and
    differs only when it was outside that range. ``rate_stated`` says whether the creator (or
    the recorded fact) named a number at all. ``target_words`` are the creator's words for the
    footage ("river", "walking"): they only choose WHICH cuts change (matched against the
    moments the plan located and the clips' analysed moments), never on-screen text. Empty
    words with no ``position`` and no ``media_id`` is a whole-edit ask ("speed up the
    footage"). ``ramp`` marks a speed ramp, which no renderer does. ``clip_number`` is the
    clip the creator named by the number the app shows on it ("the end of clip 2"); the target
    words then say WHERE in that clip (Leverkusen M2 run 3).
    """

    requirement_id: str
    rate: float
    rate_requested: float
    rate_stated: bool
    target_words: tuple[str, ...] = ()
    position: str | None = None
    media_id: str | None = None
    ramp: bool = False
    clip_number: int | None = None


def _clamped(rate: float) -> float:
    return round(min(MAX_PLAYBACK_RATE, max(MIN_PLAYBACK_RATE, rate)), 3)


def _direction(tokens: Sequence[str]) -> str | None:
    """ "slower" / "faster" from the words, or None when they say neither."""
    if "down" in tokens:
        return "slower"
    if "up" in tokens:
        return "faster"
    if _SLOW_WORDS.intersection(tokens) or any(
        token.startswith(_TR_SLOW_STEMS) for token in tokens
    ):
        return "slower"
    if _FAST_WORDS.intersection(tokens) or any(
        token.startswith(_TR_FAST_STEMS) for token in tokens
    ):
        return "faster"
    return None


def _stated_rate(numbers: Sequence[str], direction: str | None) -> float | None:
    """The rate the words name, or None: "2x" / "x2" / "1.5x", "2 kat" / "3 times", "double",
    "half speed". A multiplier on a slow ask divides ("slow down 2x" = 0.5x); a fraction is the
    rate itself whatever the direction."""
    for token in numbers:
        if token in _FRACTION_WORDS:
            return _FRACTION_WORDS[token]
    times: float | None = None
    for index, token in enumerate(numbers):
        value = _multiplier_value(token)
        if value is None and token in _TIMES_MARKERS and index > 0:
            value = _as_number(numbers[index - 1])
            if value is None:
                value = _TIMES_WORDS.get(numbers[index - 1])
        if value is None and token in _TIMES_WORDS and token not in {"two", "three", "four"}:
            value = _TIMES_WORDS[token]  # "double", "twice", "triple"
        if value is None and token in {"two", "three", "four", "iki", "uc", "dort"}:
            nxt = numbers[index + 1] if index + 1 < len(numbers) else ""
            if nxt in _TIMES_MARKERS:
                value = _TIMES_WORDS[token]
        if value is not None and value > 0 and math.isfinite(value):
            times = value
            break
    if times is None:
        return None
    if direction == "slower" and times > 1:
        return 1.0 / times
    return times


def _is_number_word(token: str) -> bool:
    return (
        _as_number(token) is not None
        or _multiplier_value(token) is not None
        or token in _TIMES_WORDS
        or token in _FRACTION_WORDS
    )


# Leverkusen M2 run 3: "at the end of clip 2" names a clip by the number the app shows on it
# ("Clip 2", "2. klip", the clip picker's own words). Singular nouns only: "2 clips" / "2 klip"
# (Turkish counts with a singular noun) are a count, never a clip.
_CLIP_NUMBER_NOUNS = frozenset({"clip", "video", "shot"})
_CLIP_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}


def _small_number(token: str) -> int | None:
    if token.isdigit() and 0 < int(token) <= 99:
        return int(token)
    return _CLIP_NUMBER_WORDS.get(token)


def _clip_noun_tr(token: str) -> bool:
    return token.startswith(("klip", "klib", "video")) and not token.startswith(
        ("klipler", "videolar")
    )


def _clip_number(text: str) -> int | None:
    """The clip the words name by its number: "clip 2" / "video two" / "2. klip" / "klip 2".
    Two different numbers name none (nothing is guessed)."""
    words = loose_text(text).replace("×", "x").split()
    bare = ["".join(ch for ch in word if ch.isalnum()) for word in words]
    found: set[int] = set()
    for index, token in enumerate(bare):
        english = token in _CLIP_NUMBER_NOUNS
        if not english and not _clip_noun_tr(token):
            continue
        nxt = bare[index + 1] if index + 1 < len(bare) else ""
        number = _small_number(nxt) if english or nxt.isdigit() else None
        if number is None and index > 0 and _clip_noun_tr(token):
            # "2. klip": only the ordinal dot makes it one clip ("2 klip" is two clips).
            prev = words[index - 1]
            if prev.endswith(".") and prev[:-1].isdigit():
                number = _small_number(prev[:-1])
        if number is not None:
            found.add(number)
    return next(iter(found)) if len(found) == 1 else None


def _target_words(tokens: Sequence[str]) -> tuple[str, ...]:
    """The creator's words for the footage: everything that is not the ask itself."""
    return tuple(
        token
        for token in tokens
        if len(token) > 1
        and not _is_number_word(token)
        and token not in _ASK_WORDS
        and not token.startswith(_TR_STEMS)
        and not token.startswith(("hizl", "yavasl"))
    )


def speed_spec(req: BriefRequirement) -> SpeedSpec | None:
    """The speed ``req`` asks for, or None when it is not a clip-speed ask."""
    if not asks_clip_speed(req):
        return None
    from app.kria.freeze_ask import _position  # noqa: PLC0415

    tokens = _tokens(req.description or "")
    numbers = _number_tokens(req.description or "")
    direction = _direction(tokens)
    recorded = _recorded_rate(req)
    if recorded is not None:
        requested: float | None = recorded
    else:
        requested = _stated_rate(numbers, direction)
    stated = requested is not None
    if requested is None:
        requested = DEFAULT_SLOW_RATE if direction == "slower" else DEFAULT_FAST_RATE
    position, used = _position(tokens)
    words = _target_words([token for index, token in enumerate(tokens) if index not in used])
    media_id = req.scope.split(":", 1)[1] if req.scope.startswith("clip:") else None
    clip_number = None if media_id else _clip_number(req.description or "")
    return SpeedSpec(
        requirement_id=req.id,
        rate=_clamped(requested),
        rate_requested=round(requested, 3),
        rate_stated=stated,
        target_words=words,
        # A moment the creator described wins over a position in the same words, and a clip
        # they named by its number decides the clip ("the end of clip 2" is inside clip 2).
        position=None if words or clip_number is not None else position,
        media_id=media_id or None,
        ramp=bool(_RAMP_WORDS.intersection(tokens)),
        clip_number=clip_number,
    )


def live_speeds(requirements: Iterable[BriefRequirement]) -> tuple[SpeedSpec, ...]:
    """Every clip-speed ask among ``requirements`` (pass ``brief.live()``), in brief order.
    Two asks that land on the same cut are settled where the cuts are grounded
    (`unified_montage._ground_speeds`): the later one wins there."""
    specs = []
    for req in requirements:
        if getattr(req, "live", True) is False:
            continue
        spec = speed_spec(req)
        if spec is not None:
            specs.append(spec)
    return tuple(specs)


__all__ = [
    "DEFAULT_FAST_RATE",
    "DEFAULT_SLOW_RATE",
    "MAX_PLAYBACK_RATE",
    "MIN_PLAYBACK_RATE",
    "RATE_FACT_KEYS",
    "SPEED_ASK_KINDS",
    "SpeedSpec",
    "asks_clip_speed",
    "live_speeds",
    "speed_spec",
]
