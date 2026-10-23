"""Did the creator ask for a freeze frame, and whose seconds does a timing ask name? (KRI-777)

"Freeze the frame for one second on Atakan's bite" (café kit M2) landed in the Creative Brief as
``timing/global {"duration_s": 1, "freeze_frame": true}``. No edit can freeze a frame: the
strategy has no field for it, the phone recipe has no hold for story footage and the cloud
renderer only clones a frame to keep a voiceover whole. `brief_checks` turns
`asks_freeze_frame` into a plain "Couldn't" on the draft (before the approval card) and on the
finished video, and never lets the model's summary promise it.

The ask's one second is not the video's length either, and neither is the seconds of a speed
ask or of "two seconds on the medal". `times_one_moment` answers that for every reader that
treats a timing requirement as the whole video's length: the receipt check, the creator-length
proof, the duration-conflict questions, the render contract and the phone planner's target.

Evidence, in order (KRI-667): a recorded fact decides. ``freeze_frame`` (or ``freeze``) is a
bool; the Main Creator is not asked for it, yet it wrote ``"freeze_frame": true`` on M2, so the
fact usually arrives. Without it the creator's words are read, deliberately narrowly and with no
``re.*`` (the KRI-667 ratchet): a phrase that can only mean holding a frame, never "frozen lake",
"freezing cold", Turkish "dondurma" (ice cream) or "hold on the sunset for 2 seconds" (a clip
length the phone can do). Delete the fallback once the model records ``freeze_frame``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.kria.brief_route import loose_text
from app.kria.speed_ask import SPEED_ASK_KINDS, asks_clip_speed

if TYPE_CHECKING:
    from app.kria.brief import BriefRequirement

# The kinds a freeze ask is filed under: timing on M2, style or select for other wordings.
FREEZE_ASK_KINDS = SPEED_ASK_KINDS
FREEZE_FACT_KEYS = ("freeze_frame", "freeze")

# Single words (diacritics folded) that only name a freeze.
_FREEZE_WORDS = frozenset({"freeze", "freezes", "freezeframe", "freezeframes"})
# Adjacent words that only mean holding one frame ("freeze-frame" folds to "freeze frame").
_FREEZE_PHRASES: tuple[tuple[str, ...], ...] = (
    ("still", "frame"),
    ("still", "frames"),
    ("hold", "the", "frame"),
    ("hold", "that", "frame"),
    ("hold", "frame"),
    ("pause", "the", "frame"),
    ("pause", "the", "video"),
    ("pause", "the", "clip"),
    # Turkish: donmuş kare, donuk kare, sabit kare
    ("donmus", "kare"),
    ("donuk", "kare"),
    ("sabit", "kare"),
)
# Turkish: "dondur" (freeze), never "dondurma" (ice cream) unless it is the verb "dondurmak".
_TR_FREEZE_STEM = "dondur"
_TR_ICE_CREAM = "dondurma"
_TR_FREEZE_INFINITIVE = "dondurmak"
# "durdur" (stop) is a freeze only when a frame or the picture is what stops ("kareyi durdur",
# "videoyu bir saniye durdur"), never "müziği durdur".
_TR_STOP_STEM = "durdur"
_TR_PICTURE = ("kare", "goruntu", "video", "sahne")

# A duration's unit; "1s" / "2sec" / "3seconds" arrive as one token. A bare "s" counts only
# after a number ("1 s"): "Atakan's" folds to "atakan s" and "it's at a café" to "it s at".
_UNITS = ("seconds", "second", "secs", "sec", "minutes", "minute")
_SHORT_UNIT = "s"
# "for one second ON Atakan's bite", "3 seconds AT the toast", "2s DURING the jump".
_MOMENT_PREPOSITIONS = frozenset({"on", "at", "during"})
_ARTICLES = frozenset({"the", "a", "an", "my", "our", "his", "her", "their", "this", "that"})
# After the preposition these name a limit or a platform, not a moment: "15 seconds at most",
# "30 seconds on TikTok".
_NOT_A_MOMENT = frozenset(
    ("most", "least", "max", "maximum", "min", "minimum", "best", "average", "total", "all")
    + ("tiktok", "instagram", "insta", "ig", "reel", "reels", "youtube", "shorts", "snapchat")
)
# Turkish: "ısırık anında 1 saniye", "madalyanın üzerinde 2 saniye", "atlayış sırasında".
_TR_UNITS = frozenset({"saniye", "saniyelik", "sn", "dakika"})
_TR_MOMENT_MARKERS = frozenset({"aninda", "uzerinde", "sirasinda", "esnasinda"})


def _tokens(text: str) -> list[str]:
    return "".join(ch if ch.isalnum() else " " for ch in loose_text(text)).split()


def _has_phrase(tokens: Sequence[str], phrases: Sequence[tuple[str, ...]]) -> bool:
    for phrase in phrases:
        width = len(phrase)
        if any(tuple(tokens[i : i + width]) == phrase for i in range(len(tokens) - width + 1)):
            return True
    return False


def _turkish_freeze(tokens: Sequence[str]) -> bool:
    for token in tokens:
        if token.startswith(_TR_FREEZE_STEM) and (
            not token.startswith(_TR_ICE_CREAM) or token.startswith(_TR_FREEZE_INFINITIVE)
        ):
            return True
    return any(token.startswith(_TR_STOP_STEM) for token in tokens) and any(
        token.startswith(_TR_PICTURE) for token in tokens
    )


def _recorded_freeze(req: BriefRequirement) -> bool | None:
    for key in FREEZE_FACT_KEYS:
        value = req.facts.get(key)
        if isinstance(value, bool):
            return value
    return None


def asks_freeze_frame(req: BriefRequirement) -> bool:
    """True when ``req`` asks the video to freeze (hold) a frame."""
    if req.kind not in FREEZE_ASK_KINDS or req.scope == "title":
        return False
    recorded = _recorded_freeze(req)
    if recorded is not None:
        return recorded
    tokens = _tokens(req.description or "")
    return (
        bool(_FREEZE_WORDS.intersection(tokens))
        or _has_phrase(tokens, _FREEZE_PHRASES)
        or _turkish_freeze(tokens)
    )


def _is_unit(tokens: Sequence[str], index: int) -> bool:
    token = tokens[index]
    if token in _UNITS:
        return True
    if token == _SHORT_UNIT:
        return index > 0 and tokens[index - 1].isdigit()
    return any(
        token.endswith(unit) and token[: -len(unit)].isdigit() for unit in (*_UNITS, _SHORT_UNIT)
    )


def _names_a_moment(tokens: Sequence[str]) -> bool:
    for index in range(len(tokens) - 1):
        if not _is_unit(tokens, index) or tokens[index + 1] not in _MOMENT_PREPOSITIONS:
            continue
        rest = [word for word in tokens[index + 2 :] if word not in _ARTICLES]
        if rest and rest[0] not in _NOT_A_MOMENT:
            return True
    return bool(_TR_UNITS.intersection(tokens)) and bool(_TR_MOMENT_MARKERS.intersection(tokens))


def times_one_moment(req: BriefRequirement) -> bool:
    """True when ``req``'s seconds belong to one moment or an effect, never the whole video.

    A freeze ("freeze the frame for one second on Atakan's bite"), a clip-speed ask ("slow
    motion for 3 seconds on the jump") and a moment's own length ("stay 2 seconds on the
    medal") all carry ``duration_s``; none of them is how long the video should be.
    """
    if asks_freeze_frame(req) or asks_clip_speed(req):
        return True
    return _names_a_moment(_tokens(req.description or ""))


# KRI-828: how long a freeze holds. The creator's own seconds win when the approved words
# name a number; otherwise one second (the receipt says so). Bounded to what a held frame
# can carry in a short-form edit without reading as a broken video.
DEFAULT_FREEZE_HOLD_S = 1.0
MIN_FREEZE_HOLD_S = 0.2
MAX_FREEZE_HOLD_S = 3.0
_NUMBER_WORDS = {
    "one": 1.0,
    "a": 1.0,
    "an": 1.0,
    "two": 2.0,
    "three": 3.0,
    "half": 0.5,
    "bir": 1.0,
    "iki": 2.0,
    "uc": 3.0,
    "yarim": 0.5,
}
# Words of the ask itself, never of the moment it names.
_ASK_WORDS = frozenset(
    {"frame", "frames", "still", "hold", "pause", "video", "clip", "picture", "shot", "image"}
    | {"for", "on", "at", "during", "when", "while", "in", "of", "and", "then", "it", "s"}
    | {"kare", "kareyi", "karede", "goruntu", "goruntuyu", "video", "videoyu", "sahne"}
    | {"donmus", "donuk", "sabit", "aninda", "uzerinde", "sirasinda", "esnasinda"}
    | {"please", "lutfen", "just", "right", "exactly", "moment", "seconds", "second"}
)


@dataclass(frozen=True)
class FreezeSpec:
    """A creator's freeze ask, read from the approved requirement (KRI-828).

    ``target_words`` are the creator's words for the moment ("atakan", "bite"): they only
    choose WHERE the frame is (matched against the clip record's analysed moments), never
    on-screen text. ``hold_s`` is how long the frame holds; ``hold_stated`` says whether the
    creator named those seconds.
    """

    requirement_id: str
    target_words: tuple[str, ...]
    hold_s: float
    hold_stated: bool


# Words that make a number-of-seconds the HOLD's length ("for 2 seconds", "freeze 2s",
# "2 saniye boyunca"), as opposed to a position ("at 14 seconds") or the video's length
# ("a 15 second video").
_HOLD_LEAD_WORDS = frozenset({"for", "lasting"}) | _FREEZE_WORDS
_TR_HOLD_TAIL_WORDS = frozenset({"boyunca", "kadar"})


def _stated_seconds(tokens: Sequence[str]) -> float | None:
    """The hold length the creator's own words give, or None.

    Only a number of seconds tied to the freeze counts: led by "for"/"freeze" in English,
    or followed by "boyunca" / the freeze verb in Turkish. Any other seconds in the ask
    (a position, the total video length) never set the hold.
    """
    for index, token in enumerate(tokens):
        if not (_is_unit(tokens, index) or token in _TR_UNITS):
            continue
        value: float | None = None
        number_at = index
        digits = "".join(ch for ch in token if ch.isdigit())
        if digits and digits != token:
            value = float(digits)  # "2s", "3seconds"
        elif index > 0:
            previous = tokens[index - 1]
            number_at = index - 1
            if previous.isdigit():
                value = float(previous)
            elif previous in {"a", "an"} and index > 1 and tokens[index - 2] == "half":
                value, number_at = 0.5, index - 2  # "half a second"
            elif previous in _NUMBER_WORDS:
                value = _NUMBER_WORDS[previous]
        if value is None:
            continue
        lead = tokens[number_at - 1] if number_at > 0 else ""
        tail = tokens[index + 1] if index + 1 < len(tokens) else ""
        if (
            lead in _HOLD_LEAD_WORDS
            or tail in _TR_HOLD_TAIL_WORDS
            or tail.startswith((_TR_FREEZE_STEM, _TR_STOP_STEM))
        ):
            return value
    return None


def freeze_spec(req: BriefRequirement) -> FreezeSpec | None:
    """The freeze ``req`` asks for, or None.

    Both must hold: the requirement is a freeze ask (`asks_freeze_frame`: a recorded fact
    or the creator's words) AND the approved words themselves read as one. A model's
    ``freeze_frame`` fact on words that ask nothing of the kind ("hold on the sunset for 2
    seconds", "about 14 seconds") never freezes a frame (KRI-821's corroboration rule).
    """
    if not asks_freeze_frame(req):
        return None
    tokens = _tokens(req.description or "")
    if not (
        _FREEZE_WORDS.intersection(tokens)
        or _has_phrase(tokens, _FREEZE_PHRASES)
        or _turkish_freeze(tokens)
    ):
        return None
    stated = _stated_seconds(tokens)
    hold = DEFAULT_FREEZE_HOLD_S
    if stated is not None:
        # The creator's own number decides the hold; a typed `duration_s` (model output)
        # never sets it.
        hold = stated
    hold = round(min(MAX_FREEZE_HOLD_S, max(MIN_FREEZE_HOLD_S, hold)), 3)
    words = tuple(
        token
        for token in tokens
        if len(token) > 1
        and not token.isdigit()
        and token not in _FREEZE_WORDS
        and token not in _ASK_WORDS
        and token not in _ARTICLES
        and token not in _NUMBER_WORDS
        and token not in _TR_UNITS
        and not token.startswith((_TR_FREEZE_STEM, _TR_STOP_STEM))
        and not any(token.startswith(unit) for unit in _UNITS)
        and not (token[:-1].isdigit() and token.endswith("s"))
    )
    return FreezeSpec(
        requirement_id=req.id,
        target_words=words,
        hold_s=hold,
        hold_stated=stated is not None,
    )


__all__ = [
    "DEFAULT_FREEZE_HOLD_S",
    "FREEZE_ASK_KINDS",
    "FREEZE_FACT_KEYS",
    "FreezeSpec",
    "asks_freeze_frame",
    "freeze_spec",
    "times_one_moment",
]
