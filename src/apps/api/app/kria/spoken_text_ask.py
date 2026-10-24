"""Did the creator ask for a text to appear when they say a word? (KRI-707)

"“Atakan” dediğimde ekrana büyük “ATAKAN” yazısı gelsin" lands in the Creative Brief as a
``text`` requirement whose ``literal`` is the text ("ATAKAN") and whose ``facts.triggers`` are
the spoken words it waits for ("Atakan"). The phone Talking worker
(`app.pipeline.phone_spoken_text`, to place the text on the word), `app.kria.brief_checks` (to
answer the requirement) and the render contract all read the ask here, never through a Main
Creator field.

What counts: a live, whole-video (``global``) text requirement with a literal and at least one
non-empty spoken trigger. A title is the opening title's own lane; an ask with no trigger
words names no moment the server could find.

KRI-791: the structured fields come first (KRI-667). When the model leaves the trigger out
(café T1: "Show MEROPE in big letters the first time she says Merope" arrived with
``facts: {}``), the ask's own wording names it: the words after its spoken cue, or the quoted
words that are not the text (`brief_checks.worded_spoken_cue`). ``speaker`` is someone other
than the creator the ask names ("she", ``facts.speaker``): the take's words don't say who
speaks, so the text shows the first time anyone says the word and the receipts say so.

KRI-724: unless the creator picked which time they mean before approval (a
``speech_occurrence`` answer, `choice_questions._speech_occurrence`). ``pin`` is read only
from the approved, server-owned answers (`brief_spoken_texts(brief, answers)`), never from a
fact the model wrote.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from app.kria.opening_cut_ask import OCCURRENCE_WAIVE, OccurrencePin, answered_occurrence_pins

if TYPE_CHECKING:
    from app.kria.brief import BriefRequirement, CreativeBrief

# Fact keys that name the spoken words a text line waits for. Narrower than
# `brief_checks._TRIGGER_FACT_KEYS`: on a text ask, "words" / "keywords" may be the text itself.
SPOKEN_TEXT_CUE_KEYS = ("triggers", "trigger", "trigger_words", "when_i_say", "spoken_words")
# Who says the trigger, when the creator names someone else ("the first time she says ...").
SPOKEN_TEXT_SPEAKER_KEY = "speaker"
# A `speaker` value that filters nothing: the creator themself, or anyone at all.
_ANY_OR_SELF = frozenset(
    {
        "i",
        "me",
        "myself",
        "we",
        "us",
        "you",
        "creator",
        "the creator",
        "anyone",
        "anybody",
        "someone",
        "somebody",
        "everyone",
        "everybody",
    }
)


@dataclass(frozen=True)
class SpokenTextAsk:
    """One text the creator wants on screen from the moment one of ``triggers`` is said."""

    requirement_id: str
    text: str
    triggers: tuple[str, ...]
    # Someone other than the creator the ask says speaks the word ("she"); None for the
    # creator's own voice or anyone's.
    speaker: str | None = None
    # KRI-724: the occurrence the creator picked ("the second 'Merope'"), from the approved
    # answers only. None = no pick: the first time anyone says it.
    pin: OccurrencePin | None = None


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


def _fact_speaker(req: BriefRequirement) -> str | None:
    value = req.facts.get(SPOKEN_TEXT_SPEAKER_KEY)
    if not isinstance(value, str):
        return None
    speaker = " ".join(value.split())
    return speaker if speaker and speaker.casefold() not in _ANY_OR_SELF else None


def requirement_spoken_text(req: BriefRequirement) -> SpokenTextAsk | None:
    """``req`` as a text to show at a spoken word, when its fields (or, without a trigger
    field, its own wording) say exactly that."""
    if not req.live or req.kind != "text" or req.scope != "global":
        return None
    text = " ".join((req.literal or "").split())
    if not text:
        return None
    from app.kria.brief_checks import worded_spoken_cue  # noqa: PLC0415

    worded_triggers, worded_speaker = worded_spoken_cue(req)
    triggers = spoken_triggers(req) or worded_triggers
    if not triggers:
        return None
    # A speaker the model wrote wins, even "I" over a "she" in the wording.
    speaker = _fact_speaker(req) if SPOKEN_TEXT_SPEAKER_KEY in req.facts else worded_speaker
    return SpokenTextAsk(requirement_id=req.id, text=text, triggers=triggers, speaker=speaker)


def spoken_text_asks(requirements: Iterable[BriefRequirement]) -> tuple[SpokenTextAsk, ...]:
    """Every text-at-a-spoken-word ask among ``requirements``, in brief order."""
    return tuple(ask for req in requirements if (ask := requirement_spoken_text(req)) is not None)


def with_answered_pins(
    asks: Iterable[SpokenTextAsk], answers: Iterable[Mapping[str, Any]] | None
) -> tuple[SpokenTextAsk, ...]:
    """``asks`` with the creator's picked occurrence on each one they answered (KRI-724).
    An ask the creator answered "leave the text off" for is dropped."""
    answers = list(answers or ())
    out: list[SpokenTextAsk] = []
    for ask in asks:
        answered = (
            answered_occurrence_pins(answers, ask.requirement_id, ask.triggers[0])
            if answers
            else None
        )
        if answered == OCCURRENCE_WAIVE:
            continue
        out.append(replace(ask, pin=answered) if isinstance(answered, OccurrencePin) else ask)
    return tuple(out)


def brief_spoken_texts(
    brief: CreativeBrief | None, answers: Iterable[Mapping[str, Any]] | None = None
) -> tuple[SpokenTextAsk, ...]:
    """`spoken_text_asks` over a brief's live requirements (empty without one), with the
    approved ``answers``' picks (`with_answered_pins`)."""
    if brief is None:
        return ()
    return with_answered_pins(spoken_text_asks(brief.live()), answers)


__all__ = [
    "SPOKEN_TEXT_CUE_KEYS",
    "SPOKEN_TEXT_SPEAKER_KEY",
    "SpokenTextAsk",
    "brief_spoken_texts",
    "requirement_spoken_text",
    "spoken_text_asks",
    "spoken_triggers",
    "with_answered_pins",
]
