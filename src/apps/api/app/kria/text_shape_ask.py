"""Did the creator ask for curved (or straight) on-screen text? (KRI-1029)

"Curved title "Plaza de Mayo 🇦🇷" on the palm trees" (Plaza de Mayo kit M1) landed in the
Creative Brief as ``text/clip:<palms> "Curved title on the palm trees"`` with no fact, and the
next turn's rewrite ("Make the title just "Plaza de Mayo"") restated it as "title on the palm
trees": the curve was gone before anything planned it, and the review still said "Done".

The phone already draws bent text (KRI-646, ``PortableTextLayer.bend``). This module turns the
ask into the brief fact ``text_shape`` (``"curved"`` / ``"straight"``) that
``brief.apply_updates`` stamps on the text requirement and keeps across rewrites
(``STICKY_FACTS``), so the montage planner can bend the text and the receipt can check it.

Evidence, in order (KRI-667): a recorded fact decides. Without it the requirement's own
description is read, by folded tokens only (no ``re.*``, the KRI-667 ratchet), and narrowly:
the shape word must sit next to a text noun ("curved title", "title should be curved",
"kavisli başlık", "title on an arc"). "Label on the curved bridge" describes the bridge, and
the literal is never read (it is the text to draw, not an instruction about it).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from app.text_fold import loose_words

if TYPE_CHECKING:
    from app.kria.brief import BriefRequirement

TEXT_SHAPE_FACT = "text_shape"
CURVED = "curved"
STRAIGHT = "straight"
TEXT_SHAPES = frozenset({CURVED, STRAIGHT})
# How far a creator's "curved" text bends on the phone (``bend`` in [-100, 100]; +100 bows
# the widest line over a half circle). A gentle rainbow arc that still reads as a title.
CURVED_TEXT_BEND = 40.0

# What on-screen text is called (diacritics folded). The shape word must touch one of these.
_TEXT_NOUNS = frozenset(
    {
        "title",
        "titles",
        "text",
        "texts",
        "label",
        "labels",
        "caption",
        "captions",
        "heading",
        "headline",
        "lettering",
        "wording",
        # Turkish: başlık, yazı, etiket (+ the possessive / plural forms a brief writes)
        "baslik",
        "basligi",
        "basliklar",
        "basliklari",
        "yazi",
        "yazisi",
        "yazilar",
        "yazilari",
        "etiket",
        "etiketi",
        "etiketler",
        "etiketleri",
    }
)
# Adjectives that only name a bent line of text (Turkish: kavisli, kıvrımlı, kıvrık).
_CURVED_WORDS = frozenset({"curved", "curvy", "arched", "bent", "kavisli", "kivrimli", "kivrik"})
# "the title curves over the trees", "curve the title".
_CURVE_VERBS = frozenset({"curve", "curves", "arc", "arcs", "arch", "arches", "bend", "bends"})
_STRAIGHT_WORDS = frozenset({"straight", "uncurved", "unbent", "kavissiz"})
# Phrases that put text on a curve: "on an arc", "in a curve"; Turkish "yay şeklinde".
_ARC_PHRASES: tuple[tuple[str, ...], ...] = (
    ("on", "an", "arc"),
    ("in", "an", "arc"),
    ("along", "an", "arc"),
    ("on", "a", "curve"),
    ("in", "a", "curve"),
    ("along", "a", "curve"),
    ("in", "an", "arch"),
    ("arc", "shaped"),
    ("yay", "seklinde"),
    ("yay", "gibi"),
    ("kavis", "seklinde"),
)
# Words between a text noun and its shape: "the title SHOULD BE curved", "title THAT IS bent".
_LINKS = frozenset(
    {"is", "be", "should", "must", "will", "to", "that", "which", "being", "looks", "look"}
)
_NEGATIONS = frozenset({"not", "no", "never", "without", "dont", "isnt", "shouldnt", "t"})
# Turkish negation after the adjective: "kavisli olmasın", "kavisli değil".
_TR_NEGATIONS = frozenset({"olmasin", "degil", "olmayacak", "olmasini"})
# One style word may sit between the shape and the noun: "curved white title".
_FILLERS = frozenset(
    {"opening", "main", "big", "large", "small", "bold", "white", "black", "yellow", "gold"}
)
# "remove the curve", "drop the arc": the shape is withdrawn whatever text it was on.
_UNDO_VERBS = frozenset({"remove", "drop", "lose", "undo", "cancel", "kaldir"})
_UNDO_OBJECTS = frozenset({"curve", "arc", "bend", "kavis", "kavisi"})


def _at(tokens: Sequence[str], index: int) -> str:
    return tokens[index] if 0 <= index < len(tokens) else ""


def _phrase_at(tokens: Sequence[str], index: int) -> int:
    """Length of the arc phrase starting at ``index`` (0 = none)."""
    for phrase in _ARC_PHRASES:
        if tuple(tokens[index : index + len(phrase)]) == phrase:
            return len(phrase)
    return 0


def _phrase_ending_at(tokens: Sequence[str], index: int) -> bool:
    """Does an arc phrase end right before ``index`` ("yay şeklinde başlık")?"""
    return any(
        tuple(tokens[max(0, index - len(phrase)) : index]) == phrase for phrase in _ARC_PHRASES
    )


def _negated(tokens: Sequence[str], index: int) -> bool:
    """A negation right before the shape word ("not curved", "no curve", "don't bend")."""
    return _at(tokens, index - 1) in _NEGATIONS or (
        _at(tokens, index - 2) in _NEGATIONS and _at(tokens, index - 1) in _LINKS
    )


def _shape_before(tokens: Sequence[str], noun: int) -> str | None:
    """The shape written before the noun at ``noun``: "curved (white) title", "curve the
    title", "yay şeklinde başlık"."""
    for gap in (1, 2):
        index = noun - gap
        if gap == 2 and _at(tokens, noun - 1) not in _FILLERS | {"the", "my", "our", "a"}:
            break
        word = _at(tokens, index)
        if word in _STRAIGHT_WORDS:
            return STRAIGHT
        if word in _CURVED_WORDS or (gap == 2 and word in _CURVE_VERBS):
            return STRAIGHT if _negated(tokens, index) else CURVED
    if _phrase_ending_at(tokens, noun):
        return CURVED
    return None


def _shape_after(tokens: Sequence[str], noun: int) -> str | None:
    """The shape written after the noun: "title curved", "title should not be curved",
    "title on an arc", "başlık kavisli olsun" / "kavisli olmasın"."""
    index = noun + 1
    negated = False
    while _at(tokens, index) in _LINKS | _NEGATIONS and index - noun <= 4:
        negated = negated or _at(tokens, index) in _NEGATIONS
        index += 1
    word = _at(tokens, index)
    if word in _STRAIGHT_WORDS:
        return STRAIGHT
    if word in _CURVED_WORDS or word in _CURVE_VERBS:
        return STRAIGHT if negated or _at(tokens, index + 1) in _TR_NEGATIONS else CURVED
    if _phrase_at(tokens, index):
        return STRAIGHT if negated else CURVED
    return None


def shape_in_words(text: str | None) -> str | None:
    """``"curved"`` / ``"straight"`` when ``text`` asks a text's shape, else None.

    Two different shapes in one sentence ("curved title and straight labels") are left
    alone (None): the words do not say which requirement each belongs to.
    """
    tokens = loose_words(text or "")
    found: set[str] = set()
    for index, token in enumerate(tokens):
        if token in _UNDO_VERBS and (
            _at(tokens, index + 1) in _UNDO_OBJECTS
            or (_at(tokens, index + 1) == "the" and _at(tokens, index + 2) in _UNDO_OBJECTS)
        ):
            found.add(STRAIGHT)
        if token not in _TEXT_NOUNS:
            continue
        for shape in (_shape_before(tokens, index), _shape_after(tokens, index)):
            if shape is not None:
                found.add(shape)
    return next(iter(found)) if len(found) == 1 else None


def recorded_shape(facts: Mapping[str, Any] | None) -> str | None:
    """The ``text_shape`` fact when it holds a known value, else None."""
    value = (facts or {}).get(TEXT_SHAPE_FACT)
    if isinstance(value, str) and value.strip().casefold() in TEXT_SHAPES:
        return value.strip().casefold()
    return None


def with_text_shape(kind: str | None, description: str | None, facts: dict[str, Any]) -> dict:
    """``facts`` with the shape this text requirement's own words ask for (KRI-1029).

    The words of this turn win over a shape carried from an earlier one ("make the title
    straight" after "curved title"); a junk recorded value is dropped. Anything that is not
    a text requirement, or says nothing about shape, is returned unchanged.
    """
    if kind != "text":
        return facts
    stated = shape_in_words(description)
    recorded = recorded_shape(facts)
    shape = stated or recorded
    if shape is None:
        if TEXT_SHAPE_FACT in facts:
            return {k: v for k, v in facts.items() if k != TEXT_SHAPE_FACT}
        return facts
    if facts.get(TEXT_SHAPE_FACT) == shape:
        return facts
    return {**facts, TEXT_SHAPE_FACT: shape}


def text_shape(req: BriefRequirement) -> str | None:
    """The shape a TEXT requirement asks for: its fact first, else its own description."""
    if req.kind != "text":
        return None
    return recorded_shape(req.facts) or shape_in_words(req.description)
