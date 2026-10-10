"""Bind each requirement of an editor turn to what the ops actually changed (KRI-558).

An editor turn compiles the creator's ops into a new draft and keeps a before/after diff of the
bundle (`services/kria_editor_ops_diff`). A requirement is `met` here when that diff holds a
change in the dimension it asked for, on the target it named, with the outcome it asked for
(the value, the direction, add vs remove, the destination, the quantity). The receipt's reason
is the change itself ("Added a fade-in animation to both texts"), so the creator sees what Kria
did instead of "I can't check this".

KRI-524 stays true: a requirement is never `met` without a real (non-derived) diff entry that
matches its dimension AND its asked outcome. A change that contradicts the ask ("Syne" when
"Inter" was asked, a quieter mix when "louder" was asked, a removal when "add" was asked) is
reported as exactly that. A requirement whose words name no dimension at all is never green:
it is shown neutrally with the changes the turn made ("I changed: ..."), because Kria cannot
tell whether they are what the creator meant.

Binding is deterministic. Ops carry no requirement ids, so a requirement is matched by the
dimensions its words and facts name, one sub-ask at a time: "Inter font and white" needs a
font change AND a colour change to be fully done.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.kria.brief import BriefRequirement
from app.kria.brief_checks import MAX_REPLY_CHARS, TextStyleRow, is_judged
from app.kria.contracts import RequirementReceipt
from app.kria.reply_language import say
from app.kria.style_asks import (
    colour_cue_families,
    derive_style_ask,
    facts_ask,
    font_from_words,
    value_matches,
    words,
)
from app.services.kria_editor_ops_diff import (
    DiffEntry,
    DiffTarget,
    EditorDiff,
    describe_diff,
)

Alts = tuple[tuple[str, frozenset[str]], ...]


def _alts(*pairs: tuple[str, Iterable[str]]) -> Alts:
    return tuple((lane, frozenset(fields)) for lane, fields in pairs)


# What each named dimension can be proven by in the diff (lane, fields); no fields = any.
_CUE_LANES: dict[str, Alts] = {
    "font": _alts(("text", ["font_family"])),
    "colour": _alts(("text", ["color", "highlight_color", "background_color"])),
    "size": _alts(("text", ["size"])),
    "animation": _alts(("text", ["entrance", "exit", "loop", "speed"])),
    "position": _alts(("text", ["position", "alignment"])),
    "case": _alts(("text", ["text_case"])),
    "spacing": _alts(("text", ["letter_spacing", "line_spacing"])),
    "shadow": _alts(("text", ["shadow_enabled"])),
    "outline": _alts(("text", ["stroke_width"])),
    "timing": _alts(("text", ["timing"])),
    "music": _alts(("audio", ["music_level", "music_track", "remove_music", "music_gain_db"])),
    "volume": _alts(("audio", ["music_level", "original_level", "music_gain_db", "remove_music"])),
    "sfx": _alts(("sfx", [])),
    "camera": _alts(("camera_effects", [])),
    "transition": _alts(("transition", [])),
    "captions": _alts(("captions", []), ("caption_meta", [])),
    "look": _alts(("timeline", ["look_preset"])),
    "speed": _alts(("timeline", ["playback_rate"])),
    "reorder": _alts(("timeline", ["order"])),
    "length": _alts(("timeline", ["duration_s", "in_s", "total_duration", "removed"])),
    "clips": _alts(("timeline", ["removed", "added"]), ("visual_media", [])),
}
# What an unlabeled requirement of each kind can be shown by (any ONE lane will do).
_KIND_LANES: dict[str, Alts] = {
    "style": _alts(
        ("text", []),
        ("transition", []),
        ("captions", []),
        ("caption_meta", []),
        ("camera_effects", []),
        ("timeline", ["look_preset", "playback_rate"]),
    ),
    "text": _alts(("text", ["wording", "added", "removed"]), ("title", [])),
    "timing": _alts(
        ("timeline", ["duration_s", "in_s", "total_duration", "removed"]),
        ("text", ["timing"]),
        ("captions", []),
    ),
    "order": _alts(("timeline", ["order"])),
    "select": _alts(("timeline", ["removed", "added"]), ("visual_media", [])),
    "audio": _alts(("audio", []), ("sfx", []), ("speech_cut", [])),
}

_REGEX_CUES: dict[str, re.Pattern[str]] = {
    "colour": re.compile(r"\b(colou?rs?|renk\w*)\b|#[0-9a-f]{6}"),
    "size": re.compile(
        r"\b(bigger|larger|smaller|size|sizes|big|small|large|tiny|huge|enlarge\w*|shrink\w*"
        r"|boyut\w*|buyut\w*|kucult\w*|buyuk|kucuk)\b"
    ),
    "case": re.compile(
        r"\b(upper ?case|lower ?case|all caps|capital\w*|title case)\b|buyuk harf|kucuk harf"
    ),
    "spacing": re.compile(r"\b(spacing|letter ?spac\w*|line ?spac\w*|aralik\w*)\b"),
    "shadow": re.compile(r"\b(shadows?|golge\w*)\b"),
    "outline": re.compile(r"\b(outline|stroke|kontur\w*)\b"),
    "music": re.compile(r"\b(music|song|track|muzik\w*|sarki\w*)\b"),
    "volume": re.compile(r"\b(volume|louder|quieter|softer|mute\w*|audio|ses\w*)\b"),
    "sfx": re.compile(r"\b(sound effects?|sfx|whoosh\w*|efekt\w*)\b"),
    "camera": re.compile(r"\b(zoom\w*|shake|camera effects?|kamera\w*)\b"),
    "transition": re.compile(r"\b(transitions?|crossfade\w*|dissolve|gecis\w*)\b"),
    "captions": re.compile(r"\b(captions?|subtitles?|altyazi\w*)\b"),
    "look": re.compile(r"\b(filters?|grade|grading|presets?|filtre\w*)\b"),
    "length": re.compile(r"\b(shorten\w*|trim\w*|duration|kisalt\w*|uzat\w*|longer|shorter)\b"),
    "timing": re.compile(r"\b(delay\w*|earlier|later|appear\w* at|disappear\w*)\b"),
}
_CLIP_NOUN = re.compile(r"\b(clips?|shots?|videos?|footage|klip\w*|cekim\w*)\b")
_TEXT_NOUN = re.compile(
    r"\b(texts?|titles?|labels?|captions?|words?|yazi\w*|baslik\w*|etiket\w*)\b"
)
_POSITION_VERB = re.compile(
    r"\b(align\w*|move\w*|position\w*|shift\w*|reposition\w*|relocate\w*|nudge\w*|line up|place"
    r"|put|konum\w*|hizala\w*|tasi\w*|ortala\w*)\b"
)
_ANIMATION = re.compile(
    r"\b(animat\w*|fade\w*|pop|pops|slide\w*|typewriter|entrance|exit|animasyon\w*|solma)\b"
)
_SPEED = re.compile(
    r"\b(slow ?down|slow ?motion|slower|faster|speed ?(up|down)|hizlan\w*|yavasla\w*)\b"
)
_FONT_VERB = re.compile(
    r"\b(change|switch|swap|replace|use|set|pick|choose)\b[a-z ]{0,25}\b(fonts?|typefaces?)\b"
)
_FONT_NOUN_ONLY = re.compile(
    r"\bfonts? (size|colou?r|weight|bigger|smaller|larger|bold)\b"
    r"|\b(bigger|smaller|larger|bold|big|small|large) fonts?\b"
)
_SAME_FONT = re.compile(r"\b(same|different|new|another|other|matching) (fonts?|typefaces?)\b")
_STOP = frozenset(
    "the and for with from that this these those them they their all both each every same "
    "make made have has had are was were will would can could should must might shall please "
    "just also then than into onto over under more less very much some any one two three text "
    "texts title titles label labels video videos clip clips font fonts color colour size "
    "animation left right top bottom center centre middle together next last first where what "
    "when which while about after before again still only like want need your you our add "
    "change remove put use set apply turn bir ve ile icin gibi daha cok tum hepsi bunu "
    "bunlari".split()
)
_REMOVE = re.compile(
    r"\b(remove\w*|delete\w*|drop|disable\w*|hide|get rid of|without|disappear\w*|kaldir\w*"
    r"|silin|sil|cikar\w*|kapat\w*)\b"
)
_ADD = re.compile(r"\b(add\w*|apply|enable\w*|insert\w*|include|ekle\w*|uygula\w*)\b")
_SMALLER_CMP = re.compile(r"\b(smaller|shrink\w*|reduc\w*|decreas\w*|kucult\w*)\b")
_BIGGER_CMP = re.compile(r"\b(bigger|larger|enlarg\w*|increas\w*|buyut\w*)\b")
_SMALL_POS = re.compile(r"\b(small|tiny|kucuk(?! harf))\b")
_BIG_POS = re.compile(r"\b(big|large|huge|buyuk(?! harf))\b")
_LOUDER = re.compile(r"\b(louder|higher|raise\w*|turn(?:ed)? up|artir\w*|yuksel\w*)\b")
_QUIETER = re.compile(r"\b(quieter|softer|lower|turn(?:ed)? down|azalt\w*|alcalt\w*)\b")
_LONGER = re.compile(r"\b(longer|lengthen\w*|extend\w*|uzat\w*)\b")
_SHORTER = re.compile(r"\b(shorter|shorten\w*|trim\w*|cut down|kisalt\w*)\b")
_QUANTIFIER = re.compile(r"\b(all|both|every|each|everything|tum\w*|butun\w*|hepsi\w*)\b")
_DESTINATION = re.compile(
    r"\b(left|right|top|bottom|center|centre|middle|sola|saga|yukari|asagi)\b"
)
_DESTINATION_MAP = {
    "left": "left",
    "sola": "left",
    "right": "right",
    "saga": "right",
    "top": "top",
    "yukari": "top",
    "bottom": "bottom",
    "asagi": "bottom",
    "center": "center",
    "centre": "center",
    "middle": "middle",
}
_ORDINALS = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
    "tenth": 10,
    "last": -1,
    "1st": 1,
    "2nd": 2,
    "3rd": 3,
    "ilk": 1,
    "ikinci": 2,
    "ucuncu": 3,
    "dorduncu": 4,
    "son": -1,
}
_ORDINAL_RE = re.compile(
    r"\b(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|last|1st|2nd|3rd"
    r"|ilk|ikinci|ucuncu|dorduncu|son)\b|\b(\d+)(?:st|nd|rd|th)\b"
    r"|\b(?:clip|shot|video) (?:number )?(\d+)\b"
)
_VALUE_FIELDS = frozenset({"font_family", "color", "entrance", "alignment", "text_case"})
# Fields where "not in the diff" means "not done" (a row that did not change did not get
# smaller), so "make all of them smaller" must cover every text.
_COVERAGE_FIELDS = frozenset({"size"})


@dataclass(frozen=True)
class _Group:
    """One sub-ask: a dimension and the (lane, fields) pairs that can prove it."""

    label: str
    alts: Alts


@dataclass(frozen=True)
class AskShape:
    groups: tuple[_Group, ...]
    # False when no word or fact named a dimension: the groups are only the kind's default
    # lanes, and such a requirement is never green (it lists what changed, neutrally).
    cued: bool = True
    target_group: str | None = None  # title | label
    target_row_ids: frozenset[str] = field(default_factory=frozenset)
    wanted: Mapping[str, str] = field(default_factory=dict)  # value asks, by diff field
    size_dir: int | None = None
    level_dir: int | None = None
    length_dir: int | None = None
    polarity: str | None = None  # add | remove
    destination: frozenset[str] = field(default_factory=frozenset)
    everything: bool = False
    live_text: int = 0
    literal: str = ""
    clip_scope: str = ""
    ordinal: int | None = None


def _fact_cues(req: BriefRequirement) -> set[str]:
    names: set[str] = set()
    facts = req.facts or {}
    ask = derive_style_ask(req)
    if ask is not None:
        names.update(
            {
                "entrance": "animation",
                "font_family": "font",
                "color": "colour",
                "alignment": "position",
                "text_case": "case",
            }[f]
            for f in ask.wanted
        )
    for key, name in (
        ("animation", "animation"),
        ("animation_phases", "animation"),
        ("position", "position"),
        ("font_family", "font"),
        ("text_color", "colour"),
    ):
        if facts.get(key) not in (None, ""):
            names.add(name)
    return names


def _named_cues(req: BriefRequirement, text: str) -> set[str]:
    names = {name for name, pattern in _REGEX_CUES.items() if pattern.search(text)}
    if "size" in names and re.search(r"(buyuk|kucuk) harf", text):
        # "buyuk harf" is a case ask; a bare "big"/"small" next to it is not a size ask.
        if not re.search(r"\b(size|sizes|bigger|larger|smaller|boyut\w*)\b", text):
            names.discard("size")
    if colour_cue_families(text):
        names.add("colour")
    elif not re.search(r"\b(colou?rs?|renk\w*)\b|#[0-9a-f]{6}", text):
        names.discard("colour")
    if font_from_words(text) or _SAME_FONT.search(text):
        names.add("font")
    elif _FONT_VERB.search(text) and not _FONT_NOUN_ONLY.search(text):
        names.add("font")
    if _ANIMATION.search(text) and not re.search(r"\b(transitions?|between|gecis\w*)\b", text):
        names.add("animation")
    clip_only = bool(_CLIP_NOUN.search(text)) and not _TEXT_NOUN.search(text)
    if _POSITION_VERB.search(text) and not clip_only:
        names.add("position")
    if _SPEED.search(text) and not re.search(r"\b(animation|exit|entrance|animasyon\w*)\b", text):
        names.add("speed")
    if re.search(r"\b(reorder\w*|swap\w*|siral\w*)\b", text) or (
        clip_only and re.search(r"\bmove\w*\b", text)
    ):
        names.add("reorder")
    names |= _fact_cues(req)
    if not names and clip_only and (_REMOVE.search(text) or _ADD.search(text)):
        names.add("clips")  # "remove the second clip": nothing else is named
    if "captions" in names and not re.search(
        r"\b(title|titles|labels?|baslik\w*|etiket\w*)\b", text
    ):
        # "make the captions bigger": the caption lane answers size / colour / font itself.
        names -= {"size", "colour", "font", "case", "animation", "shadow", "outline", "position"}
    return names


def _target_rows(text: str, rows: Sequence[TextStyleRow]) -> frozenset[str]:
    """Live rows a requirement names by their own words ("the lisbon text")."""
    if len(rows) < 2:
        return frozenset()
    tokens = {t for t in text.split() if len(t) >= 4 and t not in _STOP}
    hits: set[str] = set()
    for token in tokens:
        matching = {r.id for r in rows if re.search(rf"\b{re.escape(token)}\b", words(r.text))}
        if matching and len(matching) < len(rows):  # a word every text has names none of them
            hits |= matching
    return frozenset(hits)


def _single_direction(first: re.Pattern[str], second: re.Pattern[str], text: str) -> int | None:
    up, down = bool(first.search(text)), bool(second.search(text))
    return 1 if up and not down else -1 if down and not up else None


def _size_direction(text: str) -> int | None:
    if _BIGGER_CMP.search(text) or _SMALLER_CMP.search(text):
        # A conflicting pair of comparatives names no direction.
        return _single_direction(_BIGGER_CMP, _SMALLER_CMP, text)
    return _single_direction(_BIG_POS, _SMALL_POS, text)


def _ordinal(text: str) -> int | None:
    if not _CLIP_NOUN.search(text):
        return None
    found: set[int] = set()
    for match in _ORDINAL_RE.finditer(text):
        word, suffixed, number = match.groups()
        if word:
            found.add(_ORDINALS[word])
        elif suffixed:
            found.add(int(suffixed))
        elif number:
            found.add(int(number))
    return next(iter(found)) if len(found) == 1 else None


def _wanted(req: BriefRequirement) -> Mapping[str, str]:
    """The typed values the ask names, keyed by the diff field that must hold them."""
    if req.kind == "style":
        ask = derive_style_ask(req)
        return dict(ask.wanted) if ask is not None else {}
    return {k: v for k, v in (facts_ask(req.facts or {}) or {}).items() if k in _VALUE_FIELDS}


def ask_shape(req: BriefRequirement, rows: Sequence[TextStyleRow], live_text: int = 0) -> AskShape:
    text = words(req.description)
    named = _named_cues(req, text)
    groups = [_Group(name, _CUE_LANES[name]) for name in _CUE_LANES if name in named]
    cued = bool(groups)
    if not groups:
        lanes = _KIND_LANES.get(req.kind, ())
        groups = [_Group(req.kind, lanes)] if lanes else []
    has_title = bool(re.search(r"\b(title|baslik\w*)\b", text)) or req.scope == "title"
    has_label = bool(re.search(r"\b(labels?|etiket\w*)\b", text)) or req.scope == "per_clip"
    group = (
        "title" if has_title and not has_label else "label" if has_label and not has_title else None
    )
    row_ids = _target_rows(text, rows)
    add, remove = bool(_ADD.search(text)), bool(_REMOVE.search(text))
    destinations = (
        frozenset(_DESTINATION_MAP[m] for m in _DESTINATION.findall(text))
        if "position" in named
        else frozenset()
    )
    clip_only = bool(_CLIP_NOUN.search(text)) and not _TEXT_NOUN.search(text)
    scope_clip = req.scope.split(":", 1)[1].casefold() if req.scope.startswith("clip:") else ""
    return AskShape(
        groups=tuple(groups),
        cued=cued,
        target_group=group,
        target_row_ids=row_ids,
        wanted=_wanted(req),
        size_dir=_size_direction(text) if "size" in named else None,
        level_dir=(
            _single_direction(_LOUDER, _QUIETER, text) if named & {"music", "volume"} else None
        ),
        length_dir=_single_direction(_LONGER, _SHORTER, text) if "length" in named else None,
        polarity="add" if add and not remove else "remove" if remove and not add else None,
        destination=destinations,
        everything=bool(_QUANTIFIER.search(text))
        and not (group or row_ids or clip_only or scope_clip),
        live_text=live_text,
        literal=words(req.literal) if req.kind == "text" and req.literal else "",
        clip_scope=scope_clip,
        ordinal=_ordinal(text),
    )


# ------------------------------------------------------------------------- judging


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _where(after: Any) -> tuple[str | None, str | None]:
    """(horizontal, vertical) words for a saved position (named spot or custom x/y)."""
    if not isinstance(after, tuple) or len(after) < 3:
        return None, None
    named, x, y = after[0], _num(after[1]), _num(after[2])
    horizontal = None if x is None else "left" if x < 0.34 else "right" if x > 0.66 else "center"
    if named in ("top", "middle", "bottom"):
        return horizontal, named
    vertical = None if y is None else "top" if y < 0.34 else "bottom" if y > 0.66 else "middle"
    return horizontal, vertical


def _destination_ok(field_name: str, after: Any, wanted: frozenset[str]) -> bool:
    if field_name == "alignment":
        sides = wanted & {"left", "right", "center"}
        return not sides or after in sides
    horizontal, vertical = _where(after)
    for word in wanted:
        if word in ("left", "right", "center") and horizontal not in (None, word):
            return False
        if word in ("top", "bottom", "middle") and vertical not in (None, word):
            return False
    return True


def _sign_ok(before: Any, after: Any, direction: int) -> bool:
    b, a = _num(before), _num(after)
    return b is None or a is None or (a - b) * direction > 0


def _same_clip(scope: str, clip_id: str | None) -> bool:
    """A ``clip:<id>`` scope names this clip: the whole id, or a short form of 6+ characters."""
    if not clip_id:
        return False
    have = clip_id.casefold()
    return scope == have or (len(scope) >= 6 and scope in have)


def _text_violation(entry: DiffEntry, t: DiffTarget, shape: AskShape) -> str | None:
    name, after = entry.field, t.after
    if name in shape.wanted and name in _VALUE_FIELDS:
        held = after if isinstance(after, str) else None
        if value_matches(name, shape.wanted[name], held) is not True:
            return "value"
    if name == "size" and shape.size_dir and not _sign_ok(t.before, after, shape.size_dir):
        return "direction"
    if (
        name in ("position", "alignment")
        and shape.destination
        and not _destination_ok(name, after, shape.destination)
    ):
        return "destination"
    if (
        name in ("wording", "added")
        and shape.literal
        and shape.literal not in words(str(after or ""))
    ):
        return "literal"
    if name == "removed" and (shape.literal or shape.polarity == "add"):
        return "polarity"
    if shape.clip_scope and not _same_clip(shape.clip_scope, t.clip_id):
        return "scope"
    pol = shape.polarity
    if pol == "remove":
        if name == "added":
            return "polarity"
        if name in ("entrance", "exit", "loop") and after not in (None, "none"):
            return "polarity"
        if name in ("shadow_enabled", "stroke_width", "background_color") and after:
            return "polarity"
    if pol == "add":
        if name in ("entrance", "exit", "loop") and after in (None, "none"):
            return "polarity"
        if name in ("shadow_enabled", "stroke_width", "background_color") and not after:
            return "polarity"
    return None


def _violation(entry: DiffEntry, t: DiffTarget, shape: AskShape) -> str | None:
    """Why this changed target does NOT deliver what was asked, else None."""
    lane, name, after, pol = entry.lane, entry.field, t.after, shape.polarity
    if lane == "text":
        return _text_violation(entry, t, shape)
    if lane == "audio":
        if (
            name in ("music_level", "original_level", "music_gain_db")
            and shape.level_dir
            and not _sign_ok(t.before, after, shape.level_dir)
        ):
            return "direction"
        if pol == "remove" and (
            name == "music_track" or (name == "music_level" and (_num(after) or 0) > 0.01)
        ):
            return "polarity"
        if pol == "add" and (
            name == "remove_music" or (name == "music_level" and _num(after) == 0)
        ):
            return "polarity"
    elif lane == "timeline":
        if (
            name in ("duration_s", "total_duration")
            and shape.length_dir
            and not _sign_ok(t.before, after, shape.length_dir)
        ):
            return "direction"
        if name == "removed" and (pol == "add" or shape.length_dir == 1):
            return "polarity"
        if name == "added" and (pol == "remove" or shape.length_dir == -1):
            return "polarity"
    elif lane in ("sfx", "camera_effects", "captions"):
        if (pol == "add" and name == "removed") or (pol == "remove" and name == "added"):
            return "polarity"
    elif lane == "visual_media":
        if pol == "add":
            return "polarity"
    elif lane == "transition" and isinstance(after, tuple) and after:
        if (pol == "remove" and after[0] != "cut") or (pol == "add" and after[0] == "cut"):
            return "polarity"
    return None


def _clip_ordinal_ok(t: DiffTarget, ordinal: int) -> bool:
    number = int(re.sub(r"\D", "", t.text) or 0)
    total = _num(t.extra)
    return number == ordinal or (ordinal == -1 and total is not None and number == int(total))


def _judge(entry: DiffEntry, shape: AskShape) -> tuple[DiffEntry | None, str]:
    """The entry limited to the asked targets, and how it fares against the asked outcome."""
    targets: list[DiffTarget] = list(entry.targets)
    narrowed_by_name = False
    if entry.lane == "text":
        if shape.target_group:
            targets = [t for t in targets if t.kind == shape.target_group]
        if shape.target_row_ids:
            targets = [t for t in targets if t.id in shape.target_row_ids]
    if shape.ordinal and entry.lane == "timeline":
        if entry.field == "total_duration":
            return None, "off_target"  # a video-wide length does not answer a one-clip ask
        if entry.field != "order":
            targets = [t for t in targets if _clip_ordinal_ok(t, shape.ordinal)]
            narrowed_by_name = True
    if not targets:
        return None, "off_target"
    checked = [(t, _violation(entry, t, shape)) for t in targets]
    wrong = [t for t, why in checked if why]
    if wrong:
        codes = {why for _t, why in checked if why}
        kept = DiffEntry(entry.lane, entry.field, tuple(wrong), entry.derived)
        return kept, "wrong_value" if "value" in codes else "wrong_outcome"
    narrowed = DiffEntry(entry.lane, entry.field, tuple(targets), entry.derived)
    covers = (
        shape.everything
        and entry.lane == "text"
        and entry.field in _COVERAGE_FIELDS
        and not narrowed_by_name
        and shape.live_text > len(targets)
    )
    return narrowed, "short" if covers else "matched"


@dataclass
class _Verdict:
    matched: list[DiffEntry] = field(default_factory=list)
    # The diff's own entries the matched (narrowed) ones came from, for the "Also changed" tally.
    sources: list[DiffEntry] = field(default_factory=list)
    off_target: list[DiffEntry] = field(default_factory=list)
    wrong: list[DiffEntry] = field(default_factory=list)
    short: list[DiffEntry] = field(default_factory=list)

    def missed(self) -> bool:
        return not self.matched or bool(self.wrong or self.short)


def _fits(group: _Group, entry: DiffEntry) -> bool:
    return any(
        entry.lane == lane and (not fields or entry.field in fields) for lane, fields in group.alts
    )


def _match_group(group: _Group, shape: AskShape, entries: Iterable[DiffEntry]) -> _Verdict:
    verdict = _Verdict()
    for entry in entries:
        if not _fits(group, entry):
            continue
        narrowed, why = _judge(entry, shape)
        if narrowed is None:
            verdict.off_target.append(entry)
        elif why == "matched":
            verdict.matched.append(narrowed)
            verdict.sources.append(entry)
        elif why == "short":
            verdict.short.append(narrowed)
            verdict.sources.append(entry)
        else:
            verdict.wrong.append(narrowed)
    return verdict


_UNMET_MIN_SHARED = 2


def _unmet_reason(req: BriefRequirement, unmet: Sequence[Mapping[str, str]]) -> str | None:
    """The copilot's own reason for a request it declined, matched by shared content words."""
    wanted = {t for t in words(req.description).split() if len(t) > 2 and t not in _STOP}
    for item in unmet:
        said = {t for t in words(str(item.get("request") or "")).split() if len(t) > 2}
        said -= _STOP
        need = min(_UNMET_MIN_SHARED, len(wanted), len(said))
        shared = len(wanted & said)
        if need and shared >= need and shared / min(len(wanted), len(said)) >= 0.5:
            reason = str(item.get("reason") or "").strip()
            return reason or say(en="I couldn't do that one", tr="Bunu yapamadım")
    return None


def _phrases(entries: Iterable[DiffEntry], live_text_count: int, limit: int = 3) -> list[str]:
    return describe_diff(entries, live_text_count=live_text_count, limit=limit)


def bind_editor_receipts(
    reqs: Sequence[BriefRequirement],
    receipts: Sequence[RequirementReceipt],
    diff: EditorDiff,
    rows: Sequence[TextStyleRow],
    *,
    unmet: Sequence[Mapping[str, str]] = (),
) -> tuple[list[RequirementReceipt], list[DiffEntry]]:
    """One receipt per requirement, naming what the turn changed.

    Returns the receipts (in requirement order) and the deliberate diff entries no
    requirement claimed, for an "Also changed" line.
    """
    by_id = {r.requirement_id: r for r in receipts}
    live = diff.live_text_count
    real = diff.real()
    claimed: set[int] = set()
    shapes = {req.id: ask_shape(req, rows, live) for req in reqs}
    verdicts_by_req: dict[str, list[_Verdict]] = {}
    # Requirements whose words name a dimension claim their entries first. One with no
    # recognisable dimension only reports what is left, and shares it with its peers.
    uncued_sources: list[DiffEntry] = []
    for cued_pass in (True, False):
        pool = real if cued_pass else [e for e in real if id(e) not in claimed]
        for req in reqs:
            shape = shapes[req.id]
            if shape.cued is not cued_pass:
                continue
            verdicts = [_match_group(g, shape, pool) for g in shape.groups]
            verdicts_by_req[req.id] = verdicts
            sources = [e for v in verdicts for e in v.sources]
            if cued_pass:
                claimed.update(id(e) for e in sources)
            else:
                uncued_sources.extend(sources)
    claimed.update(id(e) for e in uncued_sources)
    out: list[RequirementReceipt] = []
    for req in reqs:
        base = by_id.get(req.id)
        if base is None:
            base = RequirementReceipt(
                requirement_id=req.id, status="partial", verification="unchecked"
            )
        shape, verdicts = shapes[req.id], verdicts_by_req[req.id]
        matched = [e for v in verdicts for e in v.matched]
        declined = _unmet_reason(req, unmet)
        if is_judged(req, base) and base.verification == "checked":
            # A value check decided it. Say what changed alongside a hit, keep a miss.
            if base.status == "met" and matched:
                phrase = "; ".join(_phrases(matched, live, 2))[:300]
                base = base.model_copy(update={"reason": phrase})
            out.append(base)
            continue
        out.append(_bind_by_diff(base, shape, verdicts, matched, declined, live))
    unbound = [e for e in real if id(e) not in claimed]
    return out, unbound


def _miss_notes(verdict: _Verdict, live: int) -> list[str]:
    notes: list[str] = []
    for entry in verdict.wrong:
        phrase = "; ".join(_phrases([entry], live, 1))
        notes.append(
            say(
                en=f"{phrase}, but that isn't what you asked for",
                tr=f"{phrase}, ama istediğin bu değil",
            )
        )
    for entry in verdict.short:
        total = max(live, len(entry.targets))
        phrase = "; ".join(_phrases([entry], live, 1))
        notes.append(
            say(
                en=f"Only {len(entry.targets)} of {total} texts changed: {phrase}",
                tr=f"{total} yazıdan yalnızca {len(entry.targets)} tanesi değişti: {phrase}",
            )
        )
    if not (verdict.wrong or verdict.short) and verdict.off_target:
        phrase = "; ".join(_phrases(verdict.off_target, 0, 1))
        notes.append(
            say(
                en=f"It changed other things, not the one you named: {phrase}",
                tr=f"Söylediğini değil, başka şeyleri değiştirdi: {phrase}",
            )
        )
    return notes


def _bind_by_diff(
    base: RequirementReceipt,
    shape: AskShape,
    verdicts: list[_Verdict],
    matched: list[DiffEntry],
    declined: str | None,
    live: int,
) -> RequirementReceipt:
    nothing = say(en="Nothing changed for this", tr="Bunun için bir şey değişmedi")
    if not shape.cued:
        # No word or fact named a dimension: say what the turn changed, never "Done".
        if matched and declined is None:
            changed = "; ".join(_phrases(matched, live, 3))
            reason = say(en=f"I changed: {changed}", tr=f"Değiştirdiklerim: {changed}")
            return base.model_copy(
                update={
                    "status": "partial",
                    "verification": "unchecked",
                    "stage": "applied",
                    "reason": reason[:300],
                }
            )
        return base.model_copy(
            update={
                "status": "not_possible",
                "verification": "checked",
                "stage": "applied",
                "reason": (declined or nothing)[:300],
            }
        )
    update: dict[str, object] = {"verification": "checked", "stage": "applied"}
    if verdicts and not any(v.missed() for v in verdicts) and declined is None:
        phrase = "; ".join(_phrases(matched, live, 2))
        return base.model_copy(update={**update, "status": "met", "reason": phrase[:300] or None})
    done = [v for v in verdicts if v.matched and not (v.wrong or v.short)]
    notes: list[str] = []
    if done:
        notes.append("; ".join(_phrases([e for v in done for e in v.matched], live, 2)))
    for verdict in verdicts:
        if verdict.missed():
            notes.extend(_miss_notes(verdict, live))
    if declined:
        notes.append(declined)
    if not notes:
        notes.append(nothing)
    partial = bool(done) or any(v.wrong or v.short for v in verdicts)
    status = "partial" if partial else "not_possible"
    return base.model_copy(update={**update, "status": status, "reason": "; ".join(notes)[:300]})


# ------------------------------------------------------------------------------ reply


def _label(receipt: RequirementReceipt) -> str:
    if receipt.verification == "unchecked":
        return say(en="Have a look", tr="Bir göz at")
    names = {
        "met": ("Done", "Yapıldı"),
        "partial": ("Partly", "Kısmen"),
        "not_possible": ("Couldn't", "Yapamadım"),
    }[receipt.status]
    return say(en=names[0], tr=names[1])


def reply_from_editor_receipts(
    reqs: Sequence[BriefRequirement],
    receipts: Sequence[RequirementReceipt],
    *,
    unbound: Sequence[str] = (),
    notices: Sequence[str] = (),
    notes: str | None = None,
) -> str:
    """What an editor turn did, from receipts and the edit diff only (never the model's summary)."""
    by_id = {req.id: req for req in reqs}
    lines: list[str] = []
    for receipt in receipts:
        req = by_id.get(receipt.requirement_id)
        if req is None:
            continue
        label = _label(receipt)
        reason = (receipt.reason or "").rstrip(".")
        if receipt.status == "met" and receipt.verification == "checked":
            lines.append(f"{label}: {reason or req.text()}")
        else:
            lines.append(f"{label}: {req.text()}" + (f" ({reason})" if reason else ""))
    if len(lines) == 1:
        body = lines[0] + ("" if lines[0].endswith((".", "!", "?", "…")) else ".")
    else:
        body = "\n".join(f"- {line}" for line in lines)
    parts = [body]
    if unbound:
        parts.append(say(en="Also changed: ", tr="Ayrıca değişti: ") + "; ".join(unbound))
    if notices:
        parts.append(" ".join(notices))
    if notes:
        parts.append(notes.strip())
    text = "\n".join(part for part in parts if part)
    return text if len(text) <= MAX_REPLY_CHARS else text[: MAX_REPLY_CHARS - 1].rstrip() + "…"


def reply_from_diff(
    phrases: Sequence[str], *, notices: Sequence[str] = (), notes: str | None = None
) -> str:
    """The reply of an editor turn that stated no checkable requirement: just what changed."""
    parts = [say(en="Done: ", tr="Yapıldı: ") + "; ".join(phrases) + "."]
    if notices:
        parts.append(" ".join(notices))
    if notes:
        parts.append(notes.strip())
    return "\n".join(parts)[:MAX_REPLY_CHARS]
