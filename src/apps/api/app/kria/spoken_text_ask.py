"""Did the creator ask for a text to appear when they say a word? (KRI-707)

"“Atakan” dediğimde ekrana büyük “ATAKAN” yazısı gelsin" lands in the Creative Brief as a
``text`` requirement whose ``literal`` is the text ("ATAKAN") and whose ``facts.triggers`` are
the spoken words it waits for ("Atakan"). The brief extractor already records both, so the
phone Talking worker (`app.pipeline.phone_spoken_text`, to place the text on the word) and
`app.kria.brief_checks` (to answer the requirement) read them off those structured fields --
never off the ask's wording (KRI-667), and never through a Main Creator field.

What counts: a live, whole-video (``global``) text requirement with a literal and at least one
non-empty spoken trigger. A title is the opening title's own lane; an ask with no trigger
words names no moment the server could find.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.kria.brief import BriefRequirement, CreativeBrief

# Fact keys that name the spoken words a text line waits for. Narrower than
# `brief_checks._TRIGGER_FACT_KEYS`: on a text ask, "words" / "keywords" may be the text itself.
SPOKEN_TEXT_CUE_KEYS = ("triggers", "trigger", "trigger_words", "when_i_say", "spoken_words")


@dataclass(frozen=True)
class SpokenTextAsk:
    """One text the creator wants on screen from the moment they say one of ``triggers``."""

    requirement_id: str
    text: str
    triggers: tuple[str, ...]


def spoken_triggers(req: BriefRequirement) -> tuple[str, ...]:
    """The spoken words ``req``'s facts name, in order, without blanks or repeats."""
    found: list[str] = []
    for key in SPOKEN_TEXT_CUE_KEYS:
        value = req.facts.get(key)
        values = value if isinstance(value, list | tuple) else [value]
        for item in values:
            if isinstance(item, str) and item.strip() and item.strip() not in found:
                found.append(item.strip())
    return tuple(found)


def requirement_spoken_text(req: BriefRequirement) -> SpokenTextAsk | None:
    """``req`` as a text to show at a spoken word, when its fields say exactly that."""
    if not req.live or req.kind != "text" or req.scope != "global":
        return None
    text = " ".join((req.literal or "").split())
    triggers = spoken_triggers(req)
    if not text or not triggers:
        return None
    return SpokenTextAsk(requirement_id=req.id, text=text, triggers=triggers)


def spoken_text_asks(requirements: Iterable[BriefRequirement]) -> tuple[SpokenTextAsk, ...]:
    """Every text-at-a-spoken-word ask among ``requirements``, in brief order."""
    return tuple(ask for req in requirements if (ask := requirement_spoken_text(req)) is not None)


def brief_spoken_texts(brief: CreativeBrief | None) -> tuple[SpokenTextAsk, ...]:
    """`spoken_text_asks` over a brief's live requirements (empty without one)."""
    return spoken_text_asks(brief.live()) if brief is not None else ()


__all__ = [
    "SPOKEN_TEXT_CUE_KEYS",
    "SpokenTextAsk",
    "brief_spoken_texts",
    "requirement_spoken_text",
    "spoken_text_asks",
    "spoken_triggers",
]
