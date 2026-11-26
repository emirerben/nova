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

import functools
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.agents._schemas.text_animation_phases import TEXT_ANIMATION_VOCAB, edge_alias_pattern
from app.config import settings
from app.kria.brief import BriefRequirement
from app.kria.brief_checks import (
    MAX_REPLY_CHARS,
    NO_MUSIC_INTENT,
    TextStyleRow,
    is_judged,
    outcome_lead,
)
from app.kria.caption_look_ask import caption_look_ask, caption_spot_holds
from app.kria.contracts import RequirementReceipt
from app.kria.reply_language import reply_language_for, say
from app.kria.style_asks import (
    colour_cue_families,
    derive_style_ask,
    facts_ask,
    font_from_words,
    value_matches,
    words,
)
from app.kria.text_trim import trim_at_word
from app.kria.voice_only_ask import asks_voice_only
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
    # KRI-1563: a caption ask that names a field is proved by that field only, never by any
    # caption change ("in the middle and a nicer style" read "Done" on a font change alone).
    "caption_position": _alts(("caption_meta", ["y_frac", "appearance"])),
    "caption_font": _alts(("caption_meta", ["font"])),
    "caption_colour": _alts(("caption_meta", ["color", "highlight_color"])),
    "caption_size": _alts(("caption_meta", ["size_px", "appearance"])),
    "caption_animation": _alts(("caption_meta", ["animation_phases", "style"])),
    "caption_shadow": _alts(("caption_meta", ["shadow_enabled"])),
    "caption_outline": _alts(("caption_meta", ["stroke_width"])),
    # No op sets the captions' letter case, so no other field may stand in for it.
    "caption_case": _alts(("caption_meta", ["text_case"])),
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
        ("timeline", ["look_preset", "playback_rate", "dim"]),
        # KRI-1563: "add pictures" names no field the brief types; what changed behind the
        # speaker (or the cards over them) is shown under it, never left out of the reply.
        ("speaker_background", []),
        ("media_overlays", []),
        ("color_moments", []),
    ),
    "text": _alts(("text", ["wording", "added", "removed"]), ("title", [])),
    "timing": _alts(
        ("timeline", ["duration_s", "in_s", "total_duration", "removed"]),
        ("text", ["timing"]),
        ("captions", []),
    ),
    "order": _alts(("timeline", ["order"])),
    "select": _alts(
        ("timeline", ["removed", "added"]),
        ("visual_media", []),
        ("speaker_background", []),
        ("media_overlays", []),
    ),
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
    "sfx": re.compile(r"\b(sound effects?|sfx|whoosh\w*|efekt\w*)\b"),
    "camera": re.compile(r"\b(zoom\w*|shake|camera effects?|kamera\w*)\b"),
    "transition": re.compile(r"\b(transitions?|crossfade\w*|dissolve|gecis\w*)\b"),
    "captions": re.compile(r"\b(captions?|subtitles?|altyazi\w*|untertitel\w*)\b"),
    "look": re.compile(r"\b(filters?|grade|grading|presets?|filtre\w*)\b"),
    "length": re.compile(r"\b(shorten\w*|trim\w*|duration|kisalt\w*|uzat\w*|longer|shorter)\b"),
    "timing": re.compile(
        r"\b(delay\w*|earlier|later|appear\w* at|disappear\w*"
        r"|gecik\w*|daha (?:gec|erken)|gec (?:cik|gel|basla|gir)\w*|erken (?:cik|gel|basla|gir)\w*"
        r"|kaybol\w*|\d+ saniye (?:sonra|once)|saniyede|belir(?:sin|sinler|ecek|di)"
        r"|(?:ekranda|daha uzun|daha kisa) (?:\w+ ){0,2}kal(?:sin|sinlar|acak|ir))\b"
    ),
}


@dataclass(frozen=True)
class _TurkishForms:
    """The patterns whose Turkish forms differ between the receipts every creator has always had
    and the ones the suggestion cards' prompts need (``KRIA_SUGGESTION_PINNED_TAPS_ENABLED``).

    The cards say "3. klibin sesini kapat" and "2. ve 3. kliplerden daha iyisini tut": Turkish
    softens a final k before a vowel (muzik -> muzigi, klip -> klibi), counts before the noun, and
    "ses efektleri" is sound effects, not the volume. ``soft`` False is the pre-card behaviour byte
    for byte (a test freezes it), so a creator without the cards gets the receipts they always did.
    """

    music: re.Pattern[str]
    volume: re.Pattern[str]
    clip_noun: re.Pattern[str]
    ordinal: re.Pattern[str]


@functools.lru_cache(maxsize=2)
def _turkish_forms(soft: bool) -> _TurkishForms:
    stem = "muzi[kg]" if soft else "muzik"
    ses = "ses(?! efekt)" if soft else "ses"
    clip = "kli[pb]" if soft else "klip"
    counted = r"|\b(\d+) (?:kli[pb]|cekim)\w*" if soft else ""
    return _TurkishForms(
        music=re.compile(rf"\b(music|song|track|{stem}\w*|sarki\w*)\b"),
        volume=re.compile(rf"\b(volume|louder|quieter|softer|mute\w*|audio|{ses}\w*)\b"),
        clip_noun=re.compile(rf"\b(clips?|shots?|videos?|footage|{clip}\w*|cekim\w*)\b"),
        ordinal=re.compile(
            r"\b(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|last|1st|2nd|3rd"
            r"|ilk|ikinci|ucuncu|dorduncu|son)\b|\b(\d+)(?:st|nd|rd|th)\b"
            r"|\b(?:clip|shot|video) (?:number )?(\d+)\b" + counted
        ),
    )


def _forms() -> _TurkishForms:
    return _turkish_forms(bool(settings.kria_suggestion_pinned_taps_enabled))


_TEXT_NOUN = re.compile(
    r"\b(texts?|titles?|labels?|captions?|words?|yazi\w*|baslik\w*|etiket\w*)\b"
)
_POSITION_VERB = re.compile(
    r"\b(align\w*|move\w*|position\w*|shift\w*|reposition\w*|relocate\w*|nudge\w*|line up|place"
    r"|put|konum\w*|hizala\w*|tasi\w*|ortala\w*)\b"
)
_ANIMATION = re.compile(
    r"\b(animat\w*|fade\w*|pop|pops|slide\w*|entrance|exit|animasyon\w*|"
    + edge_alias_pattern(TEXT_ANIMATION_VOCAB)
    + r")\b"
)
_SPEED = re.compile(
    r"\b(slow ?down|slow ?motion|slower|faster|speed ?(up|down)|hizlan\w*|yavasla\w*)\b"
)
# KRI-783: Turkish puts the verb after the noun ("yazı tipini değiştir", "fontu değiştir").
_FONT_VERB = re.compile(
    r"\b(change|switch|swap|replace|use|set|pick|choose)\b[a-z ]{0,25}\b(fonts?|typefaces?)\b"
    r"|\b(font|fontu\w*|fontlar\w*|yazi tip\w*)\b[a-z ]{0,25}"
    r"\b(degis\w*|kullan\w*|sec|secin|secil\w*|yap\w*)\b"
)
_FONT_NOUN_ONLY = re.compile(
    r"\bfonts? (size|colou?r|weight|bigger|smaller|larger|bold)\b"
    r"|\b(bigger|smaller|larger|bold|big|small|large) fonts?\b"
    r"|\b(font|yazi tipi)\w* (boyut|renk|kalinlig)\w*"
)
_SAME_FONT = re.compile(
    r"\b(same|different|new|another|other|matching) (fonts?|typefaces?)\b"
    r"|\b(ayni|farkli|yeni|baska|diger) (bir )?(font|yazi tipi)\w*"
    r"|\b(font|yazi tip)\w* (ayni|farkli|baska|degis)\w*"
)
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
_LONGER = re.compile(r"\b(longer|lengthen\w*|extend\w*|uzat\w*|daha uzun)\b")
_SHORTER = re.compile(r"\b(shorter|shorten\w*|trim\w*|cut down|kisalt\w*|daha kisa)\b")
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
# KRI-1558: "make me smaller in part 1" changes that part's layout, not the whole-video corner.
_SPEAKER_SIZE: Alts = _alts(("speaker_background", ["speaker_frame", "layout"]))
# How big each part layout draws the speaker (her own take and the full-size cut-out are both
# full size; the picture alone hides her), so a size ask's direction can be checked.
_SPEAKER_LAYOUT_SIZE = {"own": 2, "full": 2, "small": 1, "picture": 0}
# KRI-1563: a caption field named inside a caption ask, and the cue that proves it.
_CAPTION_FIELD_CUES = {
    "position": "caption_position",
    "font": "caption_font",
    "colour": "caption_colour",
    "size": "caption_size",
    "animation": "caption_animation",
    "shadow": "caption_shadow",
    "outline": "caption_outline",
    "case": "caption_case",
}
_VALUE_FIELDS = frozenset({"font_family", "color", "entrance", "alignment", "text_case"})
# Fields where "not in the diff" means "not done" (a row that did not change did not get
# smaller), so "make all of them smaller" must cover every text.
_COVERAGE_FIELDS = frozenset({"size"})


@dataclass(frozen=True)
class _Group:
    """One sub-ask: a dimension and the (lane, fields) pairs that can prove it."""

    label: str
    alts: Alts
    # KRI-1563: a caption look group beside typed caption fields only collects the other
    # caption changes for that ask's line; finding none is no miss.
    optional: bool = False


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
    # KRI-1558: the talk's parts the ask names (`facts.speaker_parts`; -1 = the last one).
    parts: frozenset[int] = field(default_factory=frozenset)
    quantified_named_scope: bool = False
    # KRI-1563: the caption look the brief writer typed (never read from the words, KRI-667).
    caption_position: str | None = None  # the typed `caption_position` (KRI-1177 vocabulary)
    caption_color: str | None = None  # the typed `caption_color` (hex)


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
    if facts.get("audio_intent") == NO_MUSIC_INTENT or asks_voice_only(req):
        names.add("music")  # the typed "no music" / "only my voice" (KRI-1164, KRI-1472)
    return names


def _caption_fact_cues(req: BriefRequirement) -> set[str]:
    """KRI-1563: the caption fields the brief writer typed on this ask (`caption_look_ask`)."""
    look = caption_look_ask(req)
    if look is None:
        return set()
    typed = {
        "caption_position": look.position,
        "caption_size": look.size,
        "caption_colour": look.color,
        "caption_case": look.case,
    }
    return {name for name, value in typed.items() if value}


def _named_cues(req: BriefRequirement, text: str) -> set[str]:
    forms = _forms()
    names = {
        name
        for name, pattern in (
            *_REGEX_CUES.items(),
            ("music", forms.music),
            ("volume", forms.volume),
        )
        if pattern.search(text)
    }
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
    clip_only = bool(forms.clip_noun.search(text)) and not _TEXT_NOUN.search(text)
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
        r"\b(title|titles|titel|labels?|baslik\w*|etiket\w*)\b", text
    ):
        # KRI-1563: a field named inside a caption ask is proved by that caption field only, never
        # by any caption change ("in the middle and a nicer style" read "Done" on a font change).
        # The place is the typed `caption_position` alone: "put captions on" names none.
        fields = {_CAPTION_FIELD_CUES[name] for name in names & _CAPTION_FIELD_CUES.keys()}
        names -= _CAPTION_FIELD_CUES.keys()
        names |= fields - {"caption_position"}
    names |= _caption_fact_cues(req)
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
    forms = _forms()
    if not forms.clip_noun.search(text):
        return None
    found: set[int] = set()
    for match in forms.ordinal.finditer(text):
        word, suffixed, number, *rest = match.groups()
        counted = rest[0] if rest else None
        if word:
            found.add(_ORDINALS[word])
        elif suffixed:
            found.add(int(suffixed))
        elif number or counted:
            found.add(int(number or counted))
    return next(iter(found)) if len(found) == 1 else None


# KRI-1558: the parts of the talk a speaker ask names, as the brief writer records them in
# `facts.speaker_parts` while speaker sections are open (1 = first part, -1 = the last). Read from
# that structured field, never guessed from the words (KRI-667).
_MAX_SPEAKER_PART = 40


def _speaker_parts(facts: Mapping[str, Any]) -> frozenset[int]:
    raw = facts.get("speaker_parts")
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(
        n
        for n in raw
        if isinstance(n, int)
        and not isinstance(n, bool)
        and (n == -1 or 1 <= n <= _MAX_SPEAKER_PART)
    )


def _part_ok(t: DiffTarget, parts: frozenset[int]) -> bool:
    """A part-layout target (``part-N``, extra ``(N, total)``) is one of the asked parts."""
    if not t.id.startswith("part-") or not isinstance(t.extra, tuple):
        return False
    number, total = int(t.extra[0]), int(t.extra[1])
    return number in parts or (-1 in parts and number == total)


def _wanted(req: BriefRequirement) -> Mapping[str, str]:
    """The typed values the ask names, keyed by the diff field that must hold them."""
    if req.kind == "style":
        ask = derive_style_ask(req)
        return dict(ask.wanted) if ask is not None else {}
    return {k: v for k, v in (facts_ask(req.facts or {}) or {}).items() if k in _VALUE_FIELDS}


def ask_shape(req: BriefRequirement, rows: Sequence[TextStyleRow], live_text: int = 0) -> AskShape:
    policy = req.facts.get("sequence_timing")
    if req.kind == "timing" and isinstance(policy, dict):
        return AskShape(
            groups=(_Group("timing", _CUE_LANES["timing"]),),
            target_row_ids=frozenset(policy.get("target_ids") or ()),
        )
    text = words(req.description)
    named = _named_cues(req, text)
    fielded = any(name.startswith("caption_") for name in named)
    # KRI-1563: a size ask that names no text or caption ("I'm left small in the corner") can be
    # about the speaker: the corner they sit in is their size on screen.
    # Only on an edit with no texts: there "smaller" cannot mean a text.
    speaker_size = (
        "size" in named
        and "captions" not in named
        and not _TEXT_NOUN.search(text)
        and live_text == 0
    )
    groups = [
        _Group(
            name,
            _CUE_LANES[name] + _SPEAKER_SIZE
            if name == "size" and speaker_size
            else _CUE_LANES[name],
            optional=name == "captions" and fielded,
        )
        for name in _CUE_LANES
        if name in named
    ]
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
    clip_only = bool(_forms().clip_noun.search(text)) and not _TEXT_NOUN.search(text)
    scope_clip = req.scope.split(":", 1)[1].casefold() if req.scope.startswith("clip:") else ""
    look = caption_look_ask(req)
    size_dir = _size_direction(text) if "size" in named else None
    if look is not None and look.size and "caption_size" in named:
        size_dir = 1 if look.size == "large" else -1
    if (req.facts.get("audio_intent") == NO_MUSIC_INTENT or asks_voice_only(req)) and not add:
        remove = True  # the typed "no music" / "only my voice" asks for the music to be gone
    return AskShape(
        caption_position=look.position if look is not None else None,
        caption_color=look.color if look is not None else None,
        groups=tuple(groups),
        cued=cued,
        target_group=group,
        target_row_ids=row_ids,
        wanted=_wanted(req),
        size_dir=size_dir,
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
        quantified_named_scope=bool(row_ids)
        and req.scope == "global"
        and bool(_QUANTIFIER.search(text))
        and not _wanted(req),
        parts=_speaker_parts(req.facts or {}),
        ordinal=(
            None
            if req.kind == "timing"
            and req.scope == "global"
            and "duration_s" in req.facts
            and "video" in text.split()
            and not clip_only
            else _ordinal(text)
        ),
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


def _speaker_size(state: Any) -> int | None:
    """The size rank of a part-layout diff value ``(layout, corner)``."""
    if isinstance(state, tuple) and state:
        return _SPEAKER_LAYOUT_SIZE.get(str(state[0]))
    return None


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


def _appearance_layers(value: Any) -> list[Mapping[str, Any]]:
    """A caption `appearance` as the layers that place lines: the whole track, then each line's
    own override (a one-line move lives only there)."""
    if not isinstance(value, Mapping):
        return []
    overrides = value.get("line_overrides")
    return [
        value,
        *(own for own in (overrides.values() if isinstance(overrides, Mapping) else ()) if own),
    ]


def _caption_ys(value: Any) -> list[float]:
    if isinstance(value, Mapping):
        found = (_num(layer.get("position_y_frac")) for layer in _appearance_layers(value))
        return [y for y in found if y is not None]
    y = _num(value)
    return [] if y is None else [y]


def _caption_violation(entry: DiffEntry, t: DiffTarget, shape: AskShape) -> str | None:
    """KRI-1563: a caption field change against what the ask named (the place, the value)."""
    name, after = entry.field, t.after
    if name in ("y_frac", "appearance"):
        for y in _caption_ys(after):
            # The band the phone writer itself holds the asked row to (KRI-1476: "middle"
            # reaches down to the free band under a face).
            if shape.caption_position and not caption_spot_holds(shape.caption_position, y):
                return "destination"
    if name == "size_px" and shape.size_dir and not _sign_ok(t.before, after, shape.size_dir):
        return "direction"
    if (
        name == "color"
        and shape.caption_color
        and str(after or "").upper() != shape.caption_color.upper()
    ):
        return "value"
    wanted = {"font": "font_family", "color": "color"}.get(name)
    if wanted and wanted in shape.wanted:
        held = after if isinstance(after, str) else None
        if value_matches(wanted, shape.wanted[wanted], held) is not True:
            return "value"
    if name == "enabled" and isinstance(after, bool):
        if (shape.polarity == "add" and not after) or (shape.polarity == "remove" and after):
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
    elif lane == "caption_meta":
        return _caption_violation(entry, t, shape)
    elif lane == "speaker_background":
        if name == "removed" and pol == "add":
            return "polarity"
        if (
            name == "layout"
            and shape.size_dir
            and not _sign_ok(_speaker_size(t.before), _speaker_size(after), shape.size_dir)
        ):
            return "direction"
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
    if shape.parts and entry.lane == "speaker_background" and entry.field == "layout":
        # KRI-1558: "make me smaller in part 1" is answered by part 1, never by part 3 alone.
        targets = [t for t in targets if _part_ok(t, shape.parts)]
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
    # The diff entry each narrowed entry was cut from (KRI-1563 counts the targets left over).
    origin: dict[int, DiffEntry] = field(default_factory=dict)

    def missed(self) -> bool:
        return not self.matched or bool(self.wrong or self.short)


def _uncertain_scope_changes(shape: AskShape, verdicts: Sequence[_Verdict]) -> list[DiffEntry]:
    """A named row plus a quantity can name a reference rather than the destination."""
    if not shape.quantified_named_scope or any(v.matched or v.wrong or v.short for v in verdicts):
        return []
    entries = {id(entry): entry for verdict in verdicts for entry in verdict.off_target}
    return list(entries.values())


# KRI-1563: one caption `appearance` entry can carry a size, a place or both; each proves
# only the asks about what it carries.
_APPEARANCE_KEYS = {
    "caption_position": frozenset({"position_x_frac", "position_y_frac"}),
    "caption_size": frozenset({"size_px"}),
}


def _fits(group: _Group, entry: DiffEntry) -> bool:
    if not any(
        entry.lane == lane and (not fields or entry.field in fields) for lane, fields in group.alts
    ):
        return False
    keys = _APPEARANCE_KEYS.get(group.label)
    if keys is not None and entry.lane == "caption_meta" and entry.field == "appearance":
        after = entry.targets[0].after if entry.targets else None
        return any(keys & layer.keys() for layer in _appearance_layers(after))
    return True


def _match_group(group: _Group, shape: AskShape, entries: Iterable[DiffEntry]) -> _Verdict:
    verdict = _Verdict()
    # KRI-1563: "in the middle and a nicer style" is two groups on the caption lane. A change
    # a named field's group judges (the place) is that group's alone, so the look group neither
    # repeats its verdict nor is sunk by it.
    fielded = [g for g in shape.groups if g.label.startswith("caption_")]
    for entry in entries:
        if not _fits(group, entry):
            continue
        if group.label == "captions" and any(_fits(g, entry) for g in fielded):
            continue
        narrowed, why = _judge(entry, shape)
        if narrowed is not None:
            verdict.origin[id(narrowed)] = entry
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


def _phrases(
    entries: Iterable[DiffEntry], live_text_count: int, limit: int | None = 3
) -> list[str]:
    return describe_diff(entries, live_text_count=live_text_count, limit=limit)


@dataclass(frozen=True)
class EditorBinding:
    """What an editor turn's receipts say and which changes each one names (KRI-1563).

    ``named`` holds, per requirement, the diff entries (narrowed to the asked targets) whose
    phrases its receipt line carries. ``unbound`` is every deliberate change no line names: a
    whole entry, or the targets an entry changed beyond what any ask named.
    """

    receipts: list[RequirementReceipt]
    named: dict[str, list[DiffEntry]]
    unbound: list[DiffEntry]


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
    bound = bind_editor_turn(reqs, receipts, diff, rows, unmet=unmet)
    return bound.receipts, bound.unbound


def _is_title_ask(req: BriefRequirement) -> bool:
    if req.kind != "text" or req.literal:
        return False
    return req.scope == "title"


def _title_entries(real: Sequence[DiffEntry], field_name: str) -> list[DiffEntry]:
    out: list[DiffEntry] = []
    for entry in real:
        if entry.lane == "title" and field_name == "added" and entry.targets[0].after:
            out.append(entry)
        elif entry.lane == "text" and entry.field == field_name:
            # A title row; else the one text the turn added (never a label, never one of many).
            titles = tuple(t for t in entry.targets if t.kind == "title")
            if not titles and len(entry.targets) == 1 and entry.targets[0].kind != "label":
                titles = entry.targets
            if titles:
                out.append(DiffEntry(entry.lane, entry.field, titles, entry.derived))
    return out


def _title_checker_misses() -> frozenset[str]:
    """Every way `_check_title` writes "the title is not the creator's", in each reply language."""
    from app.kria.brief_checks import (  # noqa: PLC0415
        _CANT_CHECK_TITLE,
        NO_TITLE_REASON,
        default_title_reason,
        reason_in_every_language,
    )
    from app.kria.reply_language import CATALOG_REPLY_LANGUAGES  # noqa: PLC0415

    default = set()
    for language in ("en", "tr", *CATALOG_REPLY_LANGUAGES):
        with reply_language_for(language):
            default.add(default_title_reason())
    return frozenset(
        {
            *reason_in_every_language(NO_TITLE_REASON),
            *reason_in_every_language(_CANT_CHECK_TITLE),
            *default,
        }
    )


def _title_by_diff(
    req: BriefRequirement, base: RequirementReceipt, real: Sequence[DiffEntry], live: int
) -> tuple[RequirementReceipt, list[DiffEntry]] | None:
    """KRI-1563 / KRI-1355: a title ask the title checker left open, settled by the diff.

    A title this turn added proves "add a title" whatever wrote its words (the checker counts
    only the creator's own words, so a title Kria wrote read "I didn't add a title"). A "no
    title" ask is met when the turn removed the title, or the draft has none. Whether
    model-written words may be applied without approval is a separate rule (KRI-1560).
    """
    from app.kria.brief_checks import (  # noqa: PLC0415
        _NO_TITLE_AS_ASKED,
        NO_TITLE_REASON,
        _loc,
        reason_in_every_language,
        title_off,
    )

    if base.status == "met" or not _is_title_ask(req):
        return None
    if base.reason not in _title_checker_misses():
        return None  # another check decided it; the diff does not overrule it
    update = {"status": "met", "verification": "checked", "stage": "applied"}
    if title_off(req):
        removed = _title_entries(real, "removed")
        if removed:
            phrase = "; ".join(_phrases(removed, live, None))
            return base.model_copy(update=update).with_reason(phrase), removed
        if base.reason in reason_in_every_language(NO_TITLE_REASON):
            return base.model_copy(update=update).with_reason(_loc(_NO_TITLE_AS_ASKED)), []
        return None
    added = _title_entries(real, "added")
    if not added:
        return None
    return base.model_copy(update=update).with_reason("; ".join(_phrases(added, live, None))), added


def _covered_by(
    entries: Iterable[DiffEntry], origins: Mapping[int, DiffEntry]
) -> set[tuple[int, int]]:
    """(diff entry, target index) pairs the given (maybe narrowed) entries name."""
    covered: set[tuple[int, int]] = set()
    for entry in entries:
        source = origins.get(id(entry), entry)
        for target in entry.targets:
            if target in source.targets:
                covered.add((id(source), source.targets.index(target)))
    return covered


def _leftovers(real: Sequence[DiffEntry], covered: set[tuple[int, int]]) -> list[DiffEntry]:
    """Each change, or the part of it, that no receipt line names."""
    out: list[DiffEntry] = []
    for entry in real:
        rest = tuple(t for i, t in enumerate(entry.targets) if (id(entry), i) not in covered)
        if len(rest) == len(entry.targets):
            out.append(entry)
        elif rest:
            out.append(DiffEntry(entry.lane, entry.field, rest, entry.derived))
    return out


def bind_editor_turn(
    reqs: Sequence[BriefRequirement],
    receipts: Sequence[RequirementReceipt],
    diff: EditorDiff,
    rows: Sequence[TextStyleRow],
    *,
    unmet: Sequence[Mapping[str, str]] = (),
    complete: bool = False,
) -> EditorBinding:
    """Bind each requirement to what the turn changed (see ``bind_editor_receipts``).

    ``complete`` is the ask ledger (KRI-1563, ``KRIA_ASK_LEDGER_ENABLED``): every change a line
    proves is named in it (no "first two" cut), and ``unbound`` is computed from what the lines
    name, so a change an ask claimed but its line never mentions is still listed. False is the
    receipts as they were, byte for byte.
    """
    limit_two = None if complete else 2
    by_id = {r.requirement_id: r for r in receipts}
    live = diff.live_text_count
    real = diff.real()
    claimed: set[int] = set()
    shapes = {req.id: ask_shape(req, rows, live) for req in reqs}
    verdicts_by_req: dict[str, list[_Verdict]] = {}
    # KRI-1563: a title the turn added (or removed, for "no title") settles a title ask the
    # checker left open, and its change is that ask's alone.
    titled: dict[str, tuple[RequirementReceipt, list[DiffEntry]]] = {}
    for req in reqs:
        base = by_id.get(req.id)
        found = _title_by_diff(req, base, real, live) if base is not None else None
        if found is not None:
            titled[req.id] = found
            claimed.update(id(_title_source(entry, real)) for entry in found[1])
    # Requirements whose words name a dimension claim their entries first. One with no
    # recognisable dimension only reports what is left, and shares it with its peers.
    uncued_sources: list[DiffEntry] = []
    for cued_pass in (True, False):
        pool = [e for e in real if id(e) not in claimed] if not cued_pass or titled else real
        for req in reqs:
            shape = shapes[req.id]
            if shape.cued is not cued_pass:
                continue
            verdicts = [_match_group(g, shape, pool) for g in shape.groups]
            verdicts_by_req[req.id] = verdicts
            if req.id in titled:
                continue
            sources = [e for v in verdicts for e in v.sources]
            if complete:
                # A change judged wrong for this ask is still this ask's: its line names it.
                sources.extend(v.origin[id(e)] for v in verdicts for e in v.wrong)
            if _unmet_reason(req, unmet) is None:
                sources.extend(_uncertain_scope_changes(shape, verdicts))
            if cued_pass:
                claimed.update(id(e) for e in sources)
            else:
                uncued_sources.extend(sources)
    claimed.update(id(e) for e in uncued_sources)
    out: list[RequirementReceipt] = []
    named: dict[str, list[DiffEntry]] = {}
    origins: dict[int, DiffEntry] = {}
    for verdicts in verdicts_by_req.values():
        for verdict in verdicts:
            origins.update(verdict.origin)
    for entry in (e for found in titled.values() for e in found[1]):
        origins[id(entry)] = _title_source(entry, real)
    for req in reqs:
        base = by_id.get(req.id)
        if base is None:
            base = RequirementReceipt(
                requirement_id=req.id, status="partial", verification="unchecked"
            )
        if req.id in titled:
            receipt, proof = titled[req.id]
            out.append(receipt)
            named[req.id] = proof
            continue
        shape, verdicts = shapes[req.id], verdicts_by_req[req.id]
        matched = [e for v in verdicts for e in v.matched]
        if isinstance(req.facts.get("sequence_timing"), dict) and is_judged(req, base):
            out.append(base)
            named[req.id] = matched if base.status == "met" else []
            continue
        declined = _unmet_reason(req, unmet)
        if is_judged(req, base) and base.verification == "checked":
            # A value check decided it. Say what changed alongside a hit, keep a miss.
            if base.status == "met" and matched:
                phrase = "; ".join(_phrases(matched, live, limit_two))
                base = base.with_reason(phrase)
                named[req.id] = matched
            else:
                named[req.id] = []
            out.append(base)
            continue
        receipt, proof = _bind_by_diff(
            base, shape, verdicts, matched, declined, live, complete=complete
        )
        out.append(receipt)
        named[req.id] = proof
    if not complete:
        return EditorBinding(out, named, [e for e in real if id(e) not in claimed])
    covered = _covered_by((e for proof in named.values() for e in proof), origins)
    return EditorBinding(out, named, _leftovers(real, covered))


def _title_source(entry: DiffEntry, real: Sequence[DiffEntry]) -> DiffEntry:
    """The diff entry a title entry was cut from (``_title_entries`` narrows text entries)."""
    for source in real:
        if (
            source.lane == entry.lane
            and source.field == entry.field
            and all(t in source.targets for t in entry.targets)
        ):
            return source
    return entry


def _miss_notes(
    verdict: _Verdict, live: int, limit: int | None = 1
) -> tuple[list[str], list[DiffEntry]]:
    notes: list[str] = []
    named: list[DiffEntry] = []
    for entry in verdict.wrong:
        phrase = "; ".join(_phrases([entry], live, 1))
        named.append(entry)
        notes.append(
            say(
                en="{phrase}, but that isn't what you asked for",
                tr="{phrase}, ama istediğin bu değil",
                phrase=phrase,
            )
        )
    for entry in verdict.short:
        total = max(live, len(entry.targets))
        phrase = "; ".join(_phrases([entry], live, 1))
        named.append(entry)
        notes.append(
            say(
                en="Only {len_entry_targets} of {total} texts changed: {phrase}",
                tr="{total} yazıdan yalnızca {len_entry_targets} tanesi değişti: {phrase}",
                len_entry_targets=len(entry.targets),
                total=total,
                phrase=phrase,
            )
        )
    if not (verdict.wrong or verdict.short) and verdict.off_target:
        phrase = "; ".join(_phrases(verdict.off_target, 0, limit))
        if limit is None:
            named.extend(verdict.off_target)
        notes.append(
            say(
                en="It changed other things, not the one you named: {phrase}",
                tr="Söylediğini değil, başka şeyleri değiştirdi: {phrase}",
                phrase=phrase,
            )
        )
    return notes, named


def _bind_by_diff(
    base: RequirementReceipt,
    shape: AskShape,
    verdicts: list[_Verdict],
    matched: list[DiffEntry],
    declined: str | None,
    live: int,
    *,
    complete: bool = False,
) -> tuple[RequirementReceipt, list[DiffEntry]]:
    """The receipt of a requirement the diff decides, and the entries its line names.

    ``complete`` (the ask ledger) names every change a line proves instead of the first two or
    three, and an ask with no named dimension that the copilot partly declined still names
    what changed for it.
    """
    one, two, three = (None, None, None) if complete else (1, 2, 3)
    nothing = say(en="Nothing changed for this", tr="Bunun için bir şey değişmedi")
    uncertain_changes = _uncertain_scope_changes(shape, verdicts)
    if uncertain_changes and declined is None:
        return base.model_copy(
            update={
                "status": "partial",
                "verification": "unchecked",
                "stage": "applied",
            }
        ).with_reason("; ".join(_phrases(uncertain_changes, live, three))), uncertain_changes
    if not shape.cued:
        # No word or fact named a dimension: say what the turn changed, never "Done".
        if matched and declined is None:
            # Just the change list: the chat shows it under a lead line, and the prose reply
            # puts "I changed:" back (``_changed_lead``).
            changed = "; ".join(_phrases(matched, live, three))
            return base.model_copy(
                update={
                    "status": "partial",
                    "verification": "unchecked",
                    "stage": "applied",
                }
            ).with_reason(changed), matched
        if matched and complete:
            # KRI-1563: "relevant pictures and not small in the corner" put her back full screen
            # and declined the pictures: say both, never "Couldn't" over a change it made.
            changed = "; ".join(_phrases(matched, live, three))
            return base.model_copy(
                update={"status": "partial", "verification": "checked", "stage": "applied"}
            ).with_reason(f"{changed}; {declined}"), matched
        return base.model_copy(
            update={
                "status": "not_possible",
                "verification": "checked",
                "stage": "applied",
            }
        ).with_reason(declined or nothing), []
    update: dict[str, object] = {"verification": "checked", "stage": "applied"}
    # An optional group (a caption look beside typed caption fields) adds its changes to the
    # line but is never a miss (KRI-1563).
    required = [v for g, v in zip(shape.groups, verdicts, strict=False) if not g.optional]
    if required and not any(v.missed() for v in required) and declined is None:
        phrase = "; ".join(_phrases(matched, live, two))
        return base.model_copy(update={**update, "status": "met"}).with_reason(
            phrase or None
        ), matched
    done = [v for v in verdicts if v.matched and not (v.wrong or v.short)]
    notes: list[str] = []
    named: list[DiffEntry] = []
    if done:
        done_entries = [e for v in done for e in v.matched]
        notes.append("; ".join(_phrases(done_entries, live, two)))
        named.extend(done_entries)
    for verdict in required:
        if verdict.missed():
            more, seen = _miss_notes(verdict, live, one)
            notes.extend(more)
            named.extend(seen)
    if declined:
        notes.append(declined)
    if not notes:
        notes.append(nothing)
    notes = list(dict.fromkeys(notes))  # two fields saying one move are one note
    partial = bool(done) or any(v.wrong or v.short for v in required)
    status = "partial" if partial else "not_possible"
    return base.model_copy(update={**update, "status": status}).with_reason("; ".join(notes)), named


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
        if reason and receipt.verification == "unchecked" and receipt.status == "partial":
            # An ask naming no dimension reports only what changed (``_bind_by_diff``).
            reason = say(en="I changed: ", tr="Değiştirdiklerim: ") + reason
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
    return trim_at_word(text, MAX_REPLY_CHARS)


def receipt_lead(
    receipts: Sequence[RequirementReceipt],
    *,
    notices: Sequence[str] = (),
    notes: str | None = None,
) -> str:
    """The one short line above an editor turn's receipt rows (KRI-739).

    The rows carry each ask's outcome and reason, so the lead only says how the turn went. The
    server's own notices and notes (time zone, a limit) follow it. The full prose stays in the
    event's ``content`` for the web chat and older apps.
    """
    if not receipts:
        return ""
    lead = outcome_lead(
        total=len(receipts),
        failed=sum(r.status == "not_possible" for r in receipts),
        completed=sum(r.status == "met" and r.verification == "checked" for r in receipts),
        partial=any(r.status == "partial" and r.verification == "checked" for r in receipts),
        unchecked=any(r.verification == "unchecked" for r in receipts),
    )
    parts = [lead, *notices, (notes or "").strip()]
    text = " ".join(part for part in parts if part)
    return trim_at_word(text, MAX_REPLY_CHARS)


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


# KRI-1563: the ask ledger matches a no-op turn's parts to the copilot's unmet list the same way.
unmet_reason = _unmet_reason
