"""Quoted start / end lines inside one take are a trim, not a clip order (KRI-789).

"Start at “Hi, I'm Karolina, I'm from Ecuador” and end right after “I'm gonna be here all
week”" asks the take to be cut to those lines. The Creative Brief records it as ONE
``timing`` requirement::

    {"kind": "timing", "scope": "global",
     "facts": {"start_line": "Hi, I'm Karolina, I'm from Ecuador",
               "end_line": "I'm gonna be here all week"}}

``start_line`` / ``end_line`` hold the creator's exact quoted words (either key alone when only
one is named). They are deliberately NOT ``start`` / ``end``: those are route keys
(``brief_route.START_KEYS``), and ``unified_montage.brief_view`` merges every requirement's
facts, so a trim stored under them would be printed as a "start → end" route title.

Merope T1 (prod 042c405c9, Main Creator v64) filed these lines as a key-less ``order``
requirement with ``start`` / ``end`` facts, which asked "You gave me a specific order for the
clips" on a one-video Talking edit. The prompt (v65) teaches the shape above;
`refile_take_trim` re-files a reply still in the old shape when the edit is one video, where
no clip order exists for the words to mean instead.

KRI-837: a one-word line ("Cheers!") is a line too, unless the reply also carries a ``key``: that
marks a clip order or a route, whose one-word start / end are places (Arnavutköy → Eminönü).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.kria.brief_route import loose_text
from app.services.spoken_quotes import MIN_QUOTE_TOKENS, quoted_texts

if TYPE_CHECKING:
    from app.kria.brief import BriefUpdate

START_LINE_FACT = "start_line"
END_LINE_FACT = "end_line"

# The only facts a start / end-lines `order` update carries. Any other one (a first / last
# clip, KRI-522; a route's distance or activity, KRI-190; a `by` / `direction` rule) makes it
# about clips or places, never a trim inside one take.
_TRIM_ORDER_FACTS = frozenset({"start", "end", "key"})
_TRIM_LINE_FACTS = (("start", START_LINE_FACT), ("end", END_LINE_FACT))


def _plain_words(text: object) -> tuple[str, ...]:
    # `loose_text` words, not `fold_tokens`: this runs while parsing the Main Creator's
    # output and must not import the grounding (render) stack.
    folded = loose_text(str(text or ""))
    return tuple("".join(ch if ch.isalnum() else " " for ch in folded).split())


def refile_take_trim(update: BriefUpdate, *, creator_message: str) -> BriefUpdate:
    """An `order` update that only names the start / end LINES of a one-video edit, as the
    ``timing`` trim it is (``facts`` {``start_line``, ``end_line``}).

    The caller passes only updates for an edit that is one video. Re-filed only when every
    named line is a run of words the creator wrote inside quote marks in ``creator_message``:
    at least one word, or at least two when the update carries a ``key`` (a clip order or a
    route, where one quoted word is a place). A pin, a route or a rule fact, an unquoted
    place, or any other fact leaves the update exactly as the model wrote it.
    """
    if update.operation == "remove" or update.kind != "order":
        return update
    facts = update.facts or {}
    if set(facts) - _TRIM_ORDER_FACTS:
        return update
    quoted = {_plain_words(text) for text in quoted_texts(creator_message)}
    floor = MIN_QUOTE_TOKENS if "key" in facts else 1
    lines: dict[str, str] = {}
    for source, target in _TRIM_LINE_FACTS:
        value = facts.get(source)
        if value is None:
            continue
        words = _plain_words(value)
        if not isinstance(value, str) or len(words) < floor or words not in quoted:
            return update
        lines[target] = value.strip()
    if not lines:
        return update
    return update.model_copy(update={"kind": "timing", "facts": lines})


__all__ = ["END_LINE_FACT", "START_LINE_FACT", "refile_take_trim"]
