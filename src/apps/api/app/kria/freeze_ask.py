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

import hashlib
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.kria.asset_holds import hold_seconds
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
    if getattr(req, "authority", None) is not None and hold_seconds(req) is not None:
        # KRI-824: creator-stamped seconds on one clip ("keep the beach photo on screen for
        # 1 second", "make the surf clip 2 seconds") are that clip's time, never the
        # video's total. (An unstamped clip-scoped length keeps its legacy reading.)
        return True
    return _names_a_moment(_tokens(req.description or ""))


# KRI-828: how long a freeze holds. The creator's own seconds win when the approved words
# name a number; otherwise one second (the receipt says so). A stated length outside these
# bounds is held at the nearest bound and the receipt says so (``hold_requested_s``): never
# a silently different length reported as met.
DEFAULT_FREEZE_HOLD_S = 1.0
MIN_FREEZE_HOLD_S = 0.2
MAX_FREEZE_HOLD_S = 10.0
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
    # KRI-900: "2 seconds into the video" names a time, never a moment.
    | {"into", "videonun"}
)

# KRI-900: a position names a cut of the edit, never a moment of the footage. "Freeze the last
# shot" is the final cut; "the last bite" is still a moment (no cut noun follows "last").
POSITION_FIRST = "first"
POSITION_LAST = "last"
_POSITION_WORDS = {
    "last": POSITION_LAST,
    "final": POSITION_LAST,
    "closing": POSITION_LAST,
    "first": POSITION_FIRST,
    "opening": POSITION_FIRST,
    # Turkish: "son kare", "ilk sahne"
    "son": POSITION_LAST,
    "ilk": POSITION_FIRST,
}
_CUT_NOUNS = frozenset(
    {"shot", "cut", "clip", "scene", "frame", "moment", "one"}
    | {"kare", "kareyi", "karede", "sahne", "sahneyi", "sahnede", "cekim", "cekimi", "klip"}
    | {"klibi", "an", "ani"}
)
# Words that are a position on their own: "at the end", "the ending", "sonunda", "başında".
_POSITION_ALONE = {
    "end": POSITION_LAST,
    "ending": POSITION_LAST,
    "sonunda": POSITION_LAST,
    "sonda": POSITION_LAST,
    "start": POSITION_FIRST,
    "beginning": POSITION_FIRST,
    "basinda": POSITION_FIRST,
    "basta": POSITION_FIRST,
}


def _position(tokens: Sequence[str]) -> tuple[str | None, frozenset[int]]:
    """The cut position ``tokens`` name ("the last shot", "at the end") and the token
    indices that say it, or ``(None, {})``. Two different positions name none."""
    found: set[str] = set()
    used: set[int] = set()
    for index, token in enumerate(tokens):
        nxt = tokens[index + 1] if index + 1 < len(tokens) else ""
        if token in _POSITION_WORDS and nxt in _CUT_NOUNS:
            found.add(_POSITION_WORDS[token])
            used.update((index, index + 1))
        elif token in _POSITION_ALONE:
            found.add(_POSITION_ALONE[token])
            used.add(index)
    if len(found) != 1:
        return None, frozenset()
    return next(iter(found)), frozenset(used)


@dataclass(frozen=True)
class FreezeSpec:
    """A creator's freeze ask, read from the approved requirement (KRI-828).

    ``target_words`` are the creator's words for the moment ("atakan", "bite"): they only
    choose WHERE the frame is (matched against the clip record's analysed moments), never
    on-screen text. ``hold_s`` is how long the frame holds; ``hold_stated`` says whether the
    creator named those seconds, and ``hold_requested_s`` is the number they named (it
    differs from ``hold_s`` only when it was outside what a freeze can hold).
    """

    requirement_id: str
    target_words: tuple[str, ...]
    hold_s: float
    hold_stated: bool
    hold_requested_s: float | None = None
    # KRI-899: the earlier live freeze asks this one corrects (latest wins per moment), so
    # their receipts follow this one's outcome. Empty for a creator's first freeze.
    corrects: tuple[str, ...] = ()
    # KRI-899: why the hold is this long: "stated" (the creator's seconds), "default" (one
    # second; the creator named none), "longer" / "shorter" (a correction with no number,
    # from ``previous_hold_s``).
    hold_basis: str = "default"
    previous_hold_s: float | None = None
    # KRI-900: "first" / "last" when the creator named a cut ("freeze the last shot") and no
    # moment. It grounds on that cut's own last frame; words for a moment always win.
    position: str | None = None

    @property
    def requirement_ids(self) -> tuple[str, ...]:
        """Every live requirement this freeze answers: the ones it corrects, then its own."""
        return (*self.corrects, self.requirement_id)


# Words that make a number-of-seconds the HOLD's length ("for 2 seconds", "freeze 2s",
# "2 saniye boyunca"), as opposed to a position ("at 14 seconds") or the video's length
# ("a 15 second video").
_HOLD_LEAD_WORDS = frozenset({"for", "lasting"}) | _FREEZE_WORDS
_TR_HOLD_TAIL_WORDS = frozenset({"boyunca", "kadar"})


def _number_tokens(text: str) -> list[str]:
    """`_tokens`, but "1.5" / "1,5" / "2.5s" stay one token (a decimal is one number)."""
    folded = loose_text(text)
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
    """A plain number token ("2", "1.5"), or None."""
    head, dot, tail = token.partition(".")
    if head.isdigit() and (not dot or tail.isdigit()):
        return float(token)
    return None


def _unit_suffixed(token: str) -> float | None:
    """ "2s", "1.5sec", "3seconds": the number before a glued unit, or None."""
    for unit in (*_UNITS, _SHORT_UNIT):
        if token.endswith(unit) and len(token) > len(unit):
            value = _as_number(token[: -len(unit)])
            if value is not None:
                return value
    return None


def _stated_seconds(tokens: Sequence[str]) -> float | None:
    """The hold length the creator's own words give, or None.

    Only a number of seconds tied to the freeze counts: led by "for"/"freeze" in English,
    or followed by "boyunca" / the freeze verb in Turkish. Any other seconds in the ask
    (a position, the total video length) never set the hold.
    """
    for index, token in enumerate(tokens):
        glued = _unit_suffixed(token)
        is_unit = token in _UNITS or token in _TR_UNITS or glued is not None
        if token == _SHORT_UNIT and index > 0 and _as_number(tokens[index - 1]) is not None:
            is_unit = True
        if not is_unit:
            continue
        value: float | None = glued
        number_at = index
        if value is None and index > 0:
            previous = tokens[index - 1]
            number_at = index - 1
            value = _as_number(previous)
            if (
                value is None
                and previous in {"a", "an"}
                and tokens[index - 2 : index - 1] == ["half"]
            ):
                value, number_at = 0.5, index - 2  # "half a second"
            elif value is None and previous in _NUMBER_WORDS:
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
    # The creator's own number decides the hold; a typed `duration_s` (model output) never
    # sets it.
    stated = _stated_seconds(_number_tokens(req.description or ""))
    return _spec(req, tokens, stated)


def _clamped(seconds: float) -> float:
    return round(min(MAX_FREEZE_HOLD_S, max(MIN_FREEZE_HOLD_S, seconds)), 3)


def _moment_words(tokens: Sequence[str]) -> tuple[str, ...]:
    """The creator's words for the moment: everything that is not the ask itself."""
    return tuple(
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


def _spec(req: BriefRequirement, tokens: Sequence[str], stated: float | None) -> FreezeSpec:
    position, used = _position(tokens)
    words = _moment_words([token for index, token in enumerate(tokens) if index not in used])
    return FreezeSpec(
        requirement_id=req.id,
        target_words=words,
        # A moment the creator described wins over a position in the same words.
        position=None if words else position,
        hold_s=_clamped(DEFAULT_FREEZE_HOLD_S if stated is None else stated),
        hold_stated=stated is not None,
        hold_requested_s=None if stated is None else round(stated, 3),
        hold_basis="default" if stated is None else "stated",
    )


# KRI-899: words of a correction to an earlier freeze ("make the freeze 2 seconds instead",
# "make it longer", "donmayı 2 saniye yap"). A freeze ask whose words are ONLY these (and the
# ask's own words) names no moment of its own: it changes the creator's latest freeze.
_CORRECTION_WORDS = frozenset(
    {"make", "change", "set", "turn", "instead", "rather", "now", "actually", "again", "but"}
    | {"longer", "shorter", "long", "short", "more", "less", "bit", "little", "lot", "much"}
    | {"too", "than", "same", "only", "to", "be", "is", "its", "can", "you", "could", "would"}
    | {"do", "let", "lets", "last", "previous", "earlier", "one", "slightly", "way", "even"}
    | {"yap", "olsun", "daha", "uzun", "kisa", "yerine", "biraz", "cok", "az", "onu", "bunu"}
    | {"sure", "suresi", "suresini", "donma", "donmayi", "donmasi", "donmasini", "degistir"}
    | {"donmus", "kareyi", "kare", "uzat", "kisalt", "simdi", "aslinda", "de", "da"}
)
_LONGER_WORDS = frozenset({"longer", "uzun", "uzat", "uzatir", "uzatin"})
_SHORTER_WORDS = frozenset({"shorter", "kisa", "kisalt", "kisaltir", "kisaltin"})
# A correction never sets the video's own length: "make the video 20 seconds" is a length ask
# even when a model files it with a stray freeze fact (KRI-821's corroboration rule).
_VIDEO_LENGTH_WORDS = frozenset(
    {"video", "videoyu", "videonun", "edit", "montage", "montaj", "reel", "whole", "total"}
    | {"overall", "entire", "tamami", "toplam", "film"}
)


def _any_seconds(tokens: Sequence[str]) -> float | None:
    """The one number of seconds a correction names ("make it 2 seconds", "2 saniye yap"),
    wherever it sits; None when it names none or more than one."""
    values: set[float] = set()
    for index, token in enumerate(tokens):
        glued = _unit_suffixed(token)
        if glued is not None:
            values.add(glued)
            continue
        if token not in _UNITS and token not in _TR_UNITS and token != _SHORT_UNIT:
            continue
        if index == 0:
            continue
        previous = tokens[index - 1]
        value = _as_number(previous)
        if value is None and previous in {"a", "an"} and tokens[index - 2 : index - 1] == ["half"]:
            value = 0.5
        elif value is None and token != _SHORT_UNIT and previous in _NUMBER_WORDS:
            value = _NUMBER_WORDS[previous]
        if value is not None:
            values.add(value)
    return next(iter(values)) if len(values) == 1 else None


def _correction(req: BriefRequirement, previous: FreezeSpec) -> FreezeSpec | None:
    """``req`` as a change to ``previous`` (the creator's latest freeze), or None.

    Only a freeze ask (`asks_freeze_frame`) that names no moment of its own counts. Its own
    freeze words are not required: the earlier freeze is the creator's corroboration
    (KRI-821), so "make it 2 seconds instead" with the model's freeze fact follows it. A
    number of seconds sets the hold; "longer" / "shorter" with no number doubles or halves
    the earlier hold; any other wording ("freeze it instead") keeps it.
    """
    if not asks_freeze_frame(req):
        return None
    tokens = _tokens(req.description or "")
    if _VIDEO_LENGTH_WORDS.intersection(tokens):
        return None
    if _position(tokens)[0] is not None:
        # KRI-900: "freeze the last shot" is a freeze of its own, never a change to an
        # earlier one ("make the last freeze 2 seconds" names no cut, so it still is).
        return None
    if any(word not in _CORRECTION_WORDS for word in _moment_words(tokens)):
        return None
    numbers = _number_tokens(req.description or "")
    stated = _stated_seconds(numbers)
    if stated is None:
        stated = _any_seconds(numbers)
    freeze_worded = bool(
        _FREEZE_WORDS.intersection(tokens)
        or _has_phrase(tokens, _FREEZE_PHRASES)
        or _turkish_freeze(tokens)
    )
    longer = bool(_LONGER_WORDS.intersection(tokens))
    shorter = bool(_SHORTER_WORDS.intersection(tokens))
    common = {
        "requirement_id": req.id,
        "target_words": previous.target_words,
        "corrects": previous.requirement_ids,
        "previous_hold_s": previous.hold_s,
        "position": previous.position,
    }
    if stated is not None:
        return FreezeSpec(
            **common,
            hold_s=_clamped(stated),
            hold_stated=True,
            hold_requested_s=round(stated, 3),
            hold_basis="stated",
        )
    if longer != shorter:
        return FreezeSpec(
            **common,
            hold_s=_clamped(previous.hold_s * (2 if longer else 0.5)),
            hold_stated=False,
            hold_basis="longer" if longer else "shorter",
        )
    if not freeze_worded:
        # Neither seconds, a direction nor the creator's own freeze words: nothing to change.
        return None
    return FreezeSpec(
        **common,
        hold_s=previous.hold_s,
        hold_stated=previous.hold_stated,
        hold_requested_s=previous.hold_requested_s,
        hold_basis=previous.hold_basis,
    )


def live_freezes(requirements: Iterable[BriefRequirement]) -> tuple[FreezeSpec, ...]:
    """KRI-899: every freeze the creator's live requirements ask for, in brief order.

    A freeze ask that names a moment is its own freeze (a second moment holds too). One that
    names no moment ("make the freeze 2 seconds instead", "make it longer") changes the
    latest freeze before it, which then answers both requirements (``corrects``). Two asks
    that name the same moment are settled where the moment is grounded
    (`unified_montage._ground_freezes`): the later one wins there.
    """
    specs: list[FreezeSpec] = []
    for req in requirements:
        if specs:
            changed = _correction(req, specs[-1])
            if changed is not None:
                specs[-1] = changed
                continue
        spec = freeze_spec(req)
        if spec is not None:
            specs.append(spec)
    return tuple(specs)


# KRI-828 / KRI-900: which analysed moment a freeze's words name. Shared by the pre-approval
# question (`choice_questions._freeze_frame`) and the worker (`unified_montage._ground_freeze`)
# so the moment the creator is asked about is the moment the render holds. Words this short
# never pick a moment.
FREEZE_MATCH_MIN_LEN = 3
_FREEZE_PREFIX_LEN = 4


@dataclass(frozen=True)
class FreezeMoment:
    """One analysed moment of one clip (``analysis.best_moments[]``). ``description`` is the
    analysis' own words: it only chooses a time and names the moment in chat, never
    on-screen text."""

    media_id: str
    start_s: float
    end_s: float
    description: str


def _match_words(text: str) -> set[str]:
    folded = loose_text(str(text))
    return {
        word
        for word in "".join(ch if ch.isalnum() else " " for ch in folded).split()
        if len(word) >= FREEZE_MATCH_MIN_LEN
    }


def _same_word(a: str, b: str) -> bool:
    if a == b:
        return True
    shorter, longer = sorted((a, b), key=len)
    return len(shorter) >= _FREEZE_PREFIX_LEN and longer.startswith(shorter)


def match_moments(
    target_words: Iterable[str], clips: Iterable[tuple[str, object]]
) -> tuple[FreezeMoment, ...]:
    """The analysed moments that best match the creator's words, ties included, in clip order.

    ``clips`` are ``(media_id, analysis)`` of the VIDEO clips in the edit. Empty when no
    moment shares a word with ``target_words``.
    """
    target = {word for word in target_words if len(word) >= FREEZE_MATCH_MIN_LEN}
    if not target:
        return ()
    scored: list[tuple[int, FreezeMoment]] = []
    for media_id, analysis in clips:
        if not isinstance(analysis, Mapping):
            continue
        for row in analysis.get("best_moments") or ():
            if not isinstance(row, Mapping):
                continue
            try:
                start, end = float(row["start_s"]), float(row["end_s"])
            except (KeyError, TypeError, ValueError):
                continue
            if not (math.isfinite(start) and math.isfinite(end) and end > start >= 0):
                continue
            description = str(row.get("description") or "")
            words = _match_words(description)
            score = sum(1 for word in target if any(_same_word(word, other) for other in words))
            if score:
                scored.append((score, FreezeMoment(str(media_id), start, end, description)))
    best = max((score for score, _moment in scored), default=0)
    seen: set[tuple[str, float, float]] = set()
    top: list[FreezeMoment] = []
    for score, moment in scored:
        key = (moment.media_id, moment.start_s, moment.end_s)
        if score == best and key not in seen:
            seen.add(key)
            top.append(moment)
    return tuple(top)


# KRI-900: the pre-approval question "which frame should hold?" and its answers. An answer's
# option key carries the exact frame, so the approved strategy's server-owned
# ``choice_answers`` is all the worker needs: ``frame_<ms>_<clip>`` (a frame the creator
# picked inside the moment), ``moment_<ms>_<clip>`` (one of several matching moments the
# creator picked; its middle frame) or ``no_freeze``.
CONFLICT_FREEZE_FRAME = "freeze_frame"
OPT_NO_FREEZE = "no_freeze"
FREEZE_BASIS_FRAME = "creator_frame"
FREEZE_BASIS_MOMENT = "creator_moment"
_OPTION_PREFIX = {"frame": FREEZE_BASIS_FRAME, "moment": FREEZE_BASIS_MOMENT}
_BASIS_PREFIX = {basis: prefix for prefix, basis in _OPTION_PREFIX.items()}


def media_key(media_id: str) -> str:
    """A short, stable name for a clip inside an option key (a media id can be longer than an
    option key may be)."""
    return hashlib.sha256(str(media_id).encode()).hexdigest()[:8]


def freeze_option_key(basis: str, media_id: str, frame_s: float) -> str:
    return f"{_BASIS_PREFIX[basis]}_{int(round(frame_s * 1000))}_{media_key(media_id)}"


def parse_freeze_option(key: object) -> tuple[str, str | None, float | None] | None:
    """``(basis, clip key, frame seconds)`` from an answer's option key, or None."""
    if key == OPT_NO_FREEZE:
        return OPT_NO_FREEZE, None, None
    parts = str(key or "").split("_")
    if len(parts) != 3 or parts[0] not in _OPTION_PREFIX or not parts[1].isdigit():
        return None
    return _OPTION_PREFIX[parts[0]], parts[2], int(parts[1]) / 1000


def planned_freeze_ids(requirements: Iterable[BriefRequirement]) -> frozenset[str]:
    """The live requirement ids a freeze plan answers (each corrected one included)."""
    return frozenset(rid for spec in live_freezes(requirements) for rid in spec.requirement_ids)


__all__ = [
    "DEFAULT_FREEZE_HOLD_S",
    "MAX_FREEZE_HOLD_S",
    "FREEZE_ASK_KINDS",
    "FREEZE_FACT_KEYS",
    "CONFLICT_FREEZE_FRAME",
    "FREEZE_BASIS_FRAME",
    "FREEZE_BASIS_MOMENT",
    "FREEZE_MATCH_MIN_LEN",
    "OPT_NO_FREEZE",
    "freeze_option_key",
    "media_key",
    "parse_freeze_option",
    "POSITION_FIRST",
    "POSITION_LAST",
    "FreezeMoment",
    "FreezeSpec",
    "asks_freeze_frame",
    "freeze_spec",
    "live_freezes",
    "match_moments",
    "planned_freeze_ids",
    "times_one_moment",
]
