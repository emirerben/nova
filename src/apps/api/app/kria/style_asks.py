"""What a creator's style, look and clip-length words ask for, as typed values (KRI-558).

A style ask used to be checkable only when the brief extractor emitted a perfect closed-vocabulary
``style_intent``; "use inter font and white" arrived as facts or plain words and ended as "can't
verify". These readers turn the three places the ask can live (``style_intent``, the facts the
extractor did fill, the creator's own words) into one small typed ask that the receipt checkers
compare against saved text rows.

Everything here is pure and conservative: when the words are ambiguous (two different colours,
two animations, a placement phrase that merely mentions "left") the answer is ``None`` and the
requirement simply gets no value check. It never guesses, so a value check can only make a
verdict stricter, not wrong.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.agents._schemas.text_element import _HEX_COLOR_RE
from app.kria.brief import (
    BriefRequirement,
    CreativeBrief,
    normalize_style_intent,
    resolve_font_name,
)
from app.kria.brief_route import loose_text
from app.schemas.text_style_intent import normalize_title_animation
from app.services.kria_editor_ops_diff import color_family, font_family_key

COLOR_FAMILY_PREFIX = "~"  # a colour WORD ("white") matches by family; a hex matches exactly


@dataclass(frozen=True)
class StyleAsk:
    """Field -> wanted value, plus the target group ("title" / "labels" / "all_text" / None)."""

    wanted: dict[str, str]
    target: str | None = None


def words(value: str | None) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", loose_text(value or "")).split())


_COLOR_WORDS = {
    "white": "white",
    "beyaz": "white",
    "black": "black",
    "siyah": "black",
    "yellow": "yellow",
    "gold": "yellow",
    "sari": "yellow",
    "red": "red",
    "kirmizi": "red",
    "blue": "blue",
    "mavi": "blue",
    "green": "green",
    "yesil": "green",
    "orange": "orange",
    "turuncu": "orange",
    "pink": "pink",
    "pembe": "pink",
    "purple": "purple",
    "mor": "purple",
    "grey": "grey",
    "gray": "grey",
    "gri": "grey",
}
_FONT_CUE = re.compile(r"\b(fonts?|fontu|fontunu|fontlar\w*|typefaces?|yazi tipi\w*)\b")
# Words that can sit between a font's name and "font" or that start the sentence.
_FONT_FILLER = frozenset({"font", "fonts", "typeface", "the", "a", "an", "to"})
_TRANSITION_WORD = re.compile(r"\b(transitions?|between|crossfade\w*|gecis\w*)\b")
_TEXT_CUE = re.compile(
    r"\b(texts?|fonts?|colou?rs?|captions?|titles?|labels?|yazi\w*|renk\w*|baslik\w*|etiket\w*)\b"
)
_ENTRANCE = (
    ("typewriter", re.compile(r"\b(type ?writer|daktilo)\b")),
    ("fade", re.compile(r"\b(fade|fades|fading|solma)\b")),
    ("pop", re.compile(r"\bpop(?: ?in| animation| effect)\b|\bpop in\b")),
    ("slide", re.compile(r"\bslide(?: ?in| ?up| ?down| animation)\b|\bkayma\b")),
)
_CASE = (
    ("upper", re.compile(r"\b(upper ?case|all caps|capital letters|buyuk harf\w*)\b")),
    ("lower", re.compile(r"\b(lower ?case|kucuk harf\w*)\b")),
    ("title", re.compile(r"\btitle case\b")),
)
_ALIGN = re.compile(
    r"\b(left|right|center|centre)[ -]?align(?:ed|ment)?\b"
    r"|\balign(?:ed)? (?:to )?(?:the )?(left|right|center|centre)\b"
    r"|\btext align (left|right|center|centre)\b"
    r"|\b(sola|saga|ortaya) hizala\w*"
)
# Words that turn "align ... left" into a PLACEMENT ask (move/line up the texts), which is
# judged from the position change, not from the alignment field.
_PLACEMENT = re.compile(
    r"\b(on|to|at) the (left|right)\b|\btogether\b|\bsame (spot|place|position|line)\b"
    r"|\bline up\b|\bhep birlikte\b|\bayni (yer|hiza)\w*"
)
_TITLE_WORD = re.compile(r"\b(title|baslik\w*)\b")
_LABEL_WORD = re.compile(r"\b(labels?|etiket\w*)\b")
_ALL_TEXTS = re.compile(
    r"\b(all|every|each|both|everything|tum|butun|hepsi\w*)\b[a-z ]{0,12}"
    r"\b(texts?|titles?|yazi\w*)\b"
    r"|\b(texts|titles|yazilar\w*)\b"
)


_COLOUR_NEXT_OK = frozenset(
    "text texts title titles label labels caption captions font fonts color colour colors colours "
    "and or please too instead everywhere for on with in yazi yazilar baslik etiket olsun yap "
    "yapin renk renkte".split()
)
_COLOUR_PREV_DESCRIBES = frozenset("the that this these those my its".split())


def colour_cue_families(text: str) -> set[str]:
    """Colour families the words ASK FOR ("make it white"), not ones that merely describe a
    thing ("the white text", "when the red car appears")."""
    tokens = text.split()
    out: set[str] = set()
    for index, token in enumerate(tokens):
        family = _COLOR_WORDS.get(token)
        if family is None:
            continue
        if index and tokens[index - 1] in _COLOUR_PREV_DESCRIBES:
            continue
        if index + 1 < len(tokens) and tokens[index + 1] not in _COLOUR_NEXT_OK:
            continue
        out.add(family)
    return out


def _distinct(pairs: list[tuple[str, re.Pattern[str]]], text: str) -> list[str]:
    return [name for name, pattern in pairs if pattern.search(text)]


def font_from_words(text: str) -> str | None:
    """The one registry font the words name ("use inter font", "alte haas font"), else None.

    Needs the word "font" (an "outfit" or a "satisfy" in a sentence is not a typeface). Windows of
    up to three words are tried longest first, each consumed once, and two different families
    in one sentence name no font at all.
    """
    if not _FONT_CUE.search(text):
        return None
    tokens = [t for t in text.split() if t not in _FONT_FILLER]
    found: dict[str | None, str] = {}
    index = 0
    while index < len(tokens):
        for size in (3, 2, 1):
            window = " ".join(tokens[index : index + size])
            resolved = resolve_font_name(window) if len(window) >= 4 else None
            if resolved:
                found.setdefault(font_family_key(resolved), resolved)
                index += size
                break
        else:
            index += 1
    return next(iter(found.values())) if len(found) == 1 else None


def _style_target(req: BriefRequirement, text: str) -> str | None:
    if req.scope == "title":
        return "title"
    has_title, has_label = bool(_TITLE_WORD.search(text)), bool(_LABEL_WORD.search(text))
    if has_title and not has_label:
        return "title"
    if has_label and not has_title:
        return "labels"
    if _ALL_TEXTS.search(text) and not (has_title or has_label):
        return "all_text"
    return None


def is_placement_ask(req: BriefRequirement) -> bool:
    """True for "align the texts on the left together": a move, not an alignment value."""
    text = words(req.description)
    return bool(_ALIGN.search(text) and _PLACEMENT.search(text))


def derive_style_ask(req: BriefRequirement) -> StyleAsk | None:
    """The typed style ask of a style requirement, else None (no value check possible)."""
    if req.kind != "style":
        return None
    intent = normalize_style_intent(req.facts.get("style_intent"))
    if intent is not None:
        return StyleAsk({i["field"]: i["value"] for i in intent["set"]}, intent.get("target"))
    wanted = facts_ask(req.facts)
    if wanted is None:
        return None
    text = words(req.description)
    explicit_alignment = bool(_ALIGN.search(text)) and not _PLACEMENT.search(text)
    fields = {
        "font_family": font_from_words(text),
        # "a fade transition between clips" is a transition ask, not a text entrance.
        "entrance": None
        if _TRANSITION_WORD.search(text)
        else _only(_distinct(list(_ENTRANCE), text)),
        "text_case": _only(_distinct(list(_CASE), text)),
    }
    for name, value in fields.items():
        if value is not None and wanted.setdefault(name, value) != value:
            return None
    colors = colour_cue_families(text)
    if len(colors) == 1 and (_TEXT_CUE.search(text) or "font_family" in wanted):
        family = next(iter(colors))
        have = wanted.setdefault("color", COLOR_FAMILY_PREFIX + family)
        if have != COLOR_FAMILY_PREFIX + family and color_family(have) != family:
            return None
    elif len(colors) > 1:
        return None
    if explicit_alignment:
        match = _ALIGN.search(text)
        side = next((g for g in match.groups() if g in ("left", "right", "center", "centre")), None)
        side = {"sola": "left", "saga": "right", "ortaya": "center"}.get(
            (match.group(4) or ""), side
        )
        if side:
            wanted.setdefault("alignment", "center" if side == "centre" else side)
    if not wanted:
        return None
    return StyleAsk(wanted, _style_target(req, text))


def _only(values: list[str]) -> str | None:
    return values[0] if len(values) == 1 else None


def facts_ask(facts: Mapping[str, Any]) -> dict[str, str] | None:
    """Typed values the extractor put in the facts; None when a fact contradicts itself."""
    from app.kria.brief import _style_value  # noqa: PLC0415

    wanted: dict[str, str] = {}
    font = facts.get("font_family")
    if isinstance(font, str) and (resolved := resolve_font_name(font)):
        wanted["font_family"] = resolved
    for key in ("text_color", "color"):
        value = facts.get(key)
        if isinstance(value, str) and _HEX_COLOR_RE.match(value.strip()):
            wanted["color"] = value.strip().upper()
            break
        if isinstance(value, str) and words(value) in _COLOR_WORDS:
            wanted["color"] = COLOR_FAMILY_PREFIX + _COLOR_WORDS[words(value)]
            break
    entrance = normalize_title_animation(facts.get("animation"))
    if entrance:
        wanted["entrance"] = entrance
    for field in ("alignment", "text_case"):
        value = _style_value(field, facts.get(field))
        if value:
            wanted[field] = value
    return wanted


def value_matches(field: str, wanted: str, have: str | None) -> bool | None:
    """True/False when the saved value decides it, None when the row does not say."""
    if have is None:
        return None
    if field == "font_family":
        return font_family_key(have) == font_family_key(wanted)
    if field == "color":
        if wanted.startswith(COLOR_FAMILY_PREFIX):
            return color_family(have) == wanted[len(COLOR_FAMILY_PREFIX) :]
        return have.upper() == wanted.upper()
    return have == wanted


# ------------------------------------------------------------------- text look (KRI-522)

_LOOK_FACTS = ("animation", "position", "font_family", "text_color")


def text_look_facts(req: BriefRequirement) -> dict[str, Any]:
    """The look facts a TEXT requirement carries (title animation, label corner, font, colour)."""
    if req.kind != "text":
        return {}
    return {key: req.facts[key] for key in _LOOK_FACTS if req.facts.get(key) not in (None, "")}


# ------------------------------------------------------------------------ clip lengths

_CLIP_NOUN = r"(?:clips?|shots?|videos?|footage|klip\w*|cekim\w*)"
_SECONDS = r"(\d+(?:xdotx\d+)?) ?(?:s|sec|secs|second|seconds|sn|saniye\w*)\b"
_PER_CLIP = re.compile(
    rf"\b(?:each|every|all|per|her)\b(?: of)?(?: the)? {_CLIP_NOUN}\b[^.]{{0,40}}?{_SECONDS}"
)
_EXCEPT = re.compile(r"\b(?:except|but|other than|besides|haric|disinda)\b(.{0,50})")


_NOT_EXACT_LENGTH = re.compile(
    r"\b(within|under|below|over|above|at most|at least|no more than|no longer than|no shorter"
    r"|up to|total|totally|together|altogether|in all|fit|toplam|en fazla|en az)\b"
)
_DECIMAL = re.compile(r"(\d)[.,](\d)")


def clip_length_ask(req: BriefRequirement) -> tuple[float, bool, bool] | None:
    """(seconds, skip first, skip last) for "make all clips 1 second except first and last".

    Only an EXACT per-clip length: "within 30 seconds total" and "every clip under 3 seconds"
    are limits, not lengths, and stay with the whole-video duration check.
    """
    if req.kind != "timing":
        return None
    # "1.5" must survive `words()`, which turns punctuation into spaces.
    text = words(_DECIMAL.sub(r"\1xdotx\2", req.description or ""))
    if _NOT_EXACT_LENGTH.search(text):
        return None
    for key in ("clip_duration_s", "per_clip_duration_s"):
        value = req.facts.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            seconds = float(value)
            break
    else:
        match = _PER_CLIP.search(text)
        if match is None:
            return None
        seconds = float(match.group(1).replace("xdotx", "."))
    if seconds <= 0:
        return None
    rest = _EXCEPT.search(text)
    tail = rest.group(1) if rest else ""
    return (
        seconds,
        bool(re.search(r"\b(first|ilk)\b", tail)),
        bool(re.search(r"\b(last|son)\b", tail)),
    )


# KRI-596: "the best six moments from the long takes, each very short" / "en iyi altı anı
# seç, her biri çok kısa". A number of moments, shots or cuts, and whether each must be
# short. "Keep it short" is the whole video's length, never a moment ask.
MOMENT_SHORT_MAX_S = 3.0
# Pieces of the finished edit: a count of these is a moment count.
_MOMENT_NOUNS = frozenset(
    {
        "moment",
        "moments",
        "shot",
        "shots",
        "cut",
        "cuts",
        "part",
        "parts",
        "scene",
        "scenes",
        "highlight",
        "highlights",
        "bit",
        "bits",
        "snippet",
        "snippets",
        "segment",
        "segments",
        "an",
        "ani",
        "anlar",
        "anlari",
        "andan",
        "sahne",
        "sahneler",
        "sahneyi",
        "sahnesi",
        "kesit",
        "kesitler",
        "kesiti",
        "parca",
        "parcalar",
        "parcayi",
    }
)
# The creator's footage: "use 5 clips" / "I filmed 10 clips" picks sources and never asks
# for that many moments.
_FOOTAGE_NOUNS = frozenset(
    {
        "clip",
        "clips",
        "video",
        "videos",
        "take",
        "takes",
        "file",
        "files",
        "recording",
        "recordings",
        "photo",
        "photos",
        "footage",
        "klip",
        "klipler",
        "klibi",
        "klipleri",
        "videolar",
        "videoyu",
        "videolari",
        "cekim",
        "cekimler",
        "kare",
        "kareler",
        "kayit",
        "kayitlar",
        "foto",
        "fotolar",
    }
)
_MOMENT_FILLERS = frozenset(
    {
        "best",
        "very",
        "short",
        "quick",
        "fast",
        "great",
        "good",
        "nice",
        "different",
        "small",
        "tiny",
        "little",
        "en",
        "iyi",
        "guzel",
        "kisa",
        "cok",
        "hizli",
        "farkli",
    }
)
# "4 of my 10 clips", "4 out of 10", "from 10 shots", "all 5 moments", "bu 10 sahne": the
# number counts the creator's footage, never the moments to cut from it.
_FOOTAGE_OWNERS = frozenset({"my", "these", "those", "all", "bu", "su", "tum", "butun", "benim"})
_COUNT_LEAD_INS = _FOOTAGE_OWNERS | {
    "the",
    "of",
    "about",
    "around",
    "like",
    "only",
    "just",
    "first",
    "last",
    "other",
}
_FOOTAGE_SOURCES = frozenset({"from", "out", "among"})
# "I filmed 10 shots" counts footage and "skip 2 moments" counts what to leave out; Turkish
# puts the verb after the noun ("10 sahne çektim", "2 anı atla").
_NOT_MOMENT_VERBS = frozenset(
    {
        "filmed",
        "recorded",
        "shot",
        "took",
        "uploaded",
        "sent",
        "added",
        "gave",
        "skip",
        "drop",
        "remove",
        "delete",
        "exclude",
        "without",
        "except",
        "ditch",
    }
)
_NOT_MOMENT_VERBS_AFTER = frozenset(
    {
        "cektim",
        "yukledim",
        "gonderdim",
        "ekledim",
        "attim",
        "var",
        "atla",
        "cikar",
        "cikart",
        "sil",
    }
)


def _counted_spans(tokens: list[str]) -> list[tuple[int, int, str | None]]:
    """Each number with its words: (index, end, noun) for "6 very short moments"."""
    spans: list[tuple[int, int, str | None]] = []
    for index, token in enumerate(tokens):
        if not token.isdigit():
            continue
        end, noun = index + 1, None
        for after in range(index + 1, min(index + 4, len(tokens))):
            if tokens[after] in _MOMENT_NOUNS or tokens[after] in _FOOTAGE_NOUNS:
                end, noun = after + 1, tokens[after]
                break
            if tokens[after] not in _MOMENT_FILLERS:
                break
            end = after + 1
        spans.append((index, end, noun))
    return spans


def _counts_footage(tokens: list[str], index: int, end: int, noun: str | None) -> bool:
    if noun in _FOOTAGE_NOUNS or (end < len(tokens) and tokens[end] in _NOT_MOMENT_VERBS_AFTER):
        return True
    back = index - 1
    passed: set[str] = set()
    while back >= 0 and tokens[back] in _COUNT_LEAD_INS:
        passed.add(tokens[back])
        back -= 1
    before = tokens[back] if back >= 0 else ""
    return bool(
        passed & _FOOTAGE_OWNERS
        or before in _FOOTAGE_SOURCES
        or before in _NOT_MOMENT_VERBS
        or ("of" in passed and before.isdigit())
    )


_SHORT_MOMENTS = re.compile(
    r"\b(?:each|every one|all)(?: of them)? (?:very |really |super )?(?:short|quick|brief)\b"
    r"|\b(?:short|quick|brief|snappy|fast) (?:moments?|shots?|cuts?|bits?|parts?|scenes?"
    r"|snippets?|highlights?|segments?)\b"
    r"|\bher biri (?:cok )?kisa\b|\bkisa (?:an|ani|anlar\w*|sahne\w*|kesit\w*|parca\w*)\b"
)


# KRI-624: the rest of a moment ask. "from the long takes" names which clips the moments
# come from; "skip the dark ones" rules some out.
_LONG_TAKES = re.compile(
    r"\bfrom (?:only )?(?:the |my )?(?:very |really )?long (?:takes?|clips?|videos?|shots?"
    r"|footage|ones)\b|\buzun (?:cekim|klip|video)\w*"
)
_MOMENT_EXCLUSION = re.compile(
    r"\b(?:skip|avoid|exclude|ignore|leave out|without|except)\b(?: (?:the|any|all))?"
    r" \w+(?: \w+)? (?:ones|clips|shots|takes|parts|moments)\b|\batla\b|\batlayarak\b"
)


@dataclass(frozen=True)
class MomentAsk:
    """How many moments the creator wants, and whether each must be short."""

    count: int | None = None
    short: bool = False
    # KRI-624: the moments must come from the long takes only.
    long_takes_only: bool = False
    # KRI-624: the ask also rules some clips out ("skip the dark ones").
    excludes: bool = False


def moment_ask(req: BriefRequirement) -> MomentAsk | None:
    from app.services.phone_reaction_grounding import fold_tokens  # noqa: PLC0415

    if req.kind == "text":
        return None
    text = req.description or ""
    tokens = fold_tokens(text)
    count: int | None = None
    fact = req.facts.get("moment_count")
    if isinstance(fact, int) and not isinstance(fact, bool) and 2 <= fact <= 30:
        count = fact
    footage: set[int] = set()
    for index, end, noun in _counted_spans(tokens):
        if _counts_footage(tokens, index, end, noun):
            footage.update(range(index, end))
        elif count is None and noun is not None and 2 <= int(tokens[index]) <= 30:
            count = int(tokens[index])
    kept = " ".join(token for index, token in enumerate(tokens) if index not in footage)
    short = bool(_SHORT_MOMENTS.search(kept))
    if count is None and not short:
        return None
    whole = " ".join(tokens)
    return MomentAsk(
        count=count,
        short=short,
        long_takes_only=bool(_LONG_TAKES.search(whole)),
        excludes=bool(_MOMENT_EXCLUSION.search(whole)),
    )


def brief_moment_ask(brief: CreativeBrief | None) -> MomentAsk | None:
    """The live moment ask: the newest count, short if any live ask says so.

    A clip-count requirement ("use the 4 clips") is no moment ask, so it can't shadow a
    later "best 6 moments, each very short"; a retracted ask stays superseded.
    """
    asks = [ask for req in (brief.live() if brief else []) if (ask := moment_ask(req))]
    if not asks:
        return None
    counts = [ask.count for ask in asks if ask.count is not None]
    return MomentAsk(
        count=counts[-1] if counts else None,
        short=any(ask.short for ask in asks),
        long_takes_only=any(ask.long_takes_only for ask in asks),
        excludes=any(ask.excludes for ask in asks),
    )
