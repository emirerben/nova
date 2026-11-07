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

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from app.kria.opening_cut_ask import OCCURRENCE_WAIVE, OccurrencePin, answered_occurrence_pins

if TYPE_CHECKING:
    from app.kria.brief import BriefRequirement, CreativeBrief

# Fact keys that name the spoken words a text line waits for. Narrower than
# `brief_checks._TRIGGER_FACT_KEYS`: on a text ask, "words" / "keywords" may be the text itself.
SPOKEN_TEXT_CUE_KEYS = ("triggers", "trigger", "trigger_words", "when_i_say", "spoken_words")
# The id every spoken-text row carries (`phone_spoken_text.spoken_text_element`). The row keeps the
# title's `generative_intro` role (the renderers and the iOS editor draw that role), so code that
# reads roles to find "the title" tells the two apart by this prefix instead (KRI-747).
SPOKEN_TEXT_ELEMENT_PREFIX = "spoken-text-"
# Fact keys a model may record a hold under; the creator's seconds in the wording count too.
SPOKEN_TEXT_HOLD_KEYS = ("hold_s", "duration_s")
# The shortest and longest hold worth honouring: below this the text is not readable, above it
# the ask is for something other than "a text on a word".
MIN_HOLD_S = 0.3
MAX_HOLD_S = 10.0
# A length counts as the text's hold only when the words around it say how long the TEXT stays
# ("for about a second", "hold 2 seconds", "bir saniye boyunca"). "within the first 5 seconds",
# "after 3 seconds", "a second later" and "for a second time" are timing or counting, not a hold.
_HOLD_LEAD = frozenset({"for", "hold", "holds", "stay", "stays", "keep", "keeps", "last", "lasts"})
_HOLD_FILLER = frozenset({"about", "around", "roughly", "approximately", "just", "only", "maybe"})
_HOLD_TRAIL = frozenset(
    {"boyunca", "süresince", "suresince", "süreyle", "sureyle"}
    | {"kalsın", "kalsin", "dursun", "kalır", "kalir"}
)
_NOT_A_HOLD_TRAIL = frozenset({"time", "times", "later", "ago"})
_TOKEN_EDGE = ".,;:!?()[]\"'“”‘’"
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
    # A structured selection among repeated trigger phrases.  ``None`` keeps
    # the legacy Talking path's first-hit rule; narrated voiceovers refuse an
    # unqualified repeat rather than inventing a moment.
    occurrence: str | int | None = None
    # KRI-972: how long the creator asked the text to stay ("for about a second" -> 1.0). None =
    # they named no length: the worker's default (`phone_spoken_text.HOLD_S`).
    hold_s: float | None = None


def is_spoken_text_bar(bar: Mapping[str, Any]) -> bool:
    """``bar`` (a text row or editor text bar) is the text-on-a-spoken-word row, not a title."""
    return str(bar.get("id") or "").startswith(SPOKEN_TEXT_ELEMENT_PREFIX)


def _hold_in_range(value: float) -> float | None:
    return round(value, 3) if math.isfinite(value) and MIN_HOLD_S <= value <= MAX_HOLD_S else None


def _fact_seconds(value: object) -> float | None:
    """A recorded length as a number: a number, or a model's numeric string ("1.5"). Not a bool."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().replace(",", "."))
        except ValueError:
            return None
    return None


def with_valid_hold_fact(facts: Mapping[str, Any]) -> dict[str, Any]:
    """``facts`` of a text update with ``hold_s`` as a number in range, or without it.

    The boundary for the typed hold the brief writer records (KRI-1292): "1" becomes 1.0, and a
    value that is not a length a text can hold (a word, zero, a negative, 400, NaN) is dropped
    rather than stored. ``spoken_text_hold_s`` then reads the creator's wording, which still
    states a length, and else the default applies: the same answer for a bad value wherever it
    arrives."""
    out = dict(facts)
    if "hold_s" in out:
        hold = _fact_seconds(out["hold_s"])
        hold = None if hold is None else _hold_in_range(hold)
        if hold is None:
            del out["hold_s"]
        else:
            out["hold_s"] = hold
    return out


def _hold_tokens(key: str) -> list[tuple[int, int, str]]:
    tokens: list[tuple[int, int, str]] = []
    position = 0
    for part in key.split():
        start = key.index(part, position)
        position = start + len(part)
        tokens.append((start, position, part.strip(_TOKEN_EDGE)))
    return tokens


def _attached_to_the_text(tokens: list[tuple[int, int, str]], start: int, end: int) -> bool:
    """The seconds at ``start:end`` are how long the text stays: a hold word right before them
    ("for", "hold", "stay", "keep", "last", with "about" / "just" between) or a hold word right
    after ("boyunca", "kalsin"), and not "a second time" / "a second later"."""
    before = [word for _s, token_end, word in tokens if token_end <= start]
    after = [word for token_start, _e, word in tokens if token_start >= end]
    if after and after[0] in _NOT_A_HOLD_TRAIL:
        return False
    if after and after[0] in _HOLD_TRAIL:
        return True
    index = len(before)
    while index and before[index - 1] in _HOLD_FILLER:
        index -= 1
    return index > 0 and before[index - 1] in _HOLD_LEAD


def _held_lengths(wording: str, literal: str) -> set[float]:
    """Every length in ``wording`` that is attached to how long the text stays."""
    from app.kria.asset_holds import _quoted_spans  # noqa: PLC0415 - import cycle
    from app.kria.planner import _hold_key, _stated_holds  # noqa: PLC0415 - import cycle
    from app.schemas.edit_proposal import seconds_negated  # noqa: PLC0415

    key = _hold_key(wording)
    copy_key = _hold_key(literal)
    if copy_key:
        key = key.replace(copy_key, " ")
    tokens = _hold_tokens(key)
    quoted = _quoted_spans(key)
    spans = [
        (start, end, value)
        for start, end, value in _stated_holds(key)
        if not any(lo <= start and end <= hi for lo, hi in quoted)
        and not seconds_negated(key, start, end)
    ]
    for index in range(1, len(tokens)):
        # "a second" holds no digit, so the seconds reader does not see it; it is 1 s.
        if tokens[index][2] in ("second", "sec") and tokens[index - 1][2] == "a":
            start, end = tokens[index - 1][0], tokens[index][1]
            if not any(lo < end and start < hi for lo, hi, _v in spans):
                spans.append((start, end, 1.0))
    return {
        float(value) for start, end, value in spans if _attached_to_the_text(tokens, start, end)
    }


def spoken_text_hold_s(req: BriefRequirement) -> float | None:
    """The seconds ``req`` asks its text to stay on screen, or None when it names none.

    A typed ``hold_s`` fact the brief writer records (KRI-1292) decides when it is a length a
    text can hold (0.3 to 10 s). One that is not (a word, 400) is no length: the brief drops it
    when it is written, and a requirement that still carries one reads like one without it. The
    wording then decides, for these and for briefs stored before the field existed (a stored
    brief is re-read on every retry and reburn): the one length the wording attaches to how long
    the text stays ("for about a second", "for 3 seconds", "bir saniye boyunca"). Seconds that
    only time the word ("within the first 5 seconds"), count something ("for a second time")
    or sit inside the on-screen copy ("3 SECONDS LEFT") are not a hold; two different holds in
    one ask name none."""
    for key in SPOKEN_TEXT_HOLD_KEYS:
        seconds = _fact_seconds(req.facts.get(key))
        hold = None if seconds is None else _hold_in_range(seconds)
        if hold is not None:
            return hold
    lengths = _held_lengths(req.description or "", req.literal or "")
    return _hold_in_range(next(iter(lengths))) if len(lengths) == 1 else None


def _fact_occurrence(req: BriefRequirement) -> str | int | None:
    value = req.facts.get("occurrence")
    if isinstance(value, str) and value in {"first", "last"}:
        return value
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    if isinstance(value, str) and value.isdecimal() and int(value) >= 1:
        return int(value)
    return None


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
    # Only the structured field chooses among repeats (KRI-667: no regex reads the
    # creator's wording for it). Without it, a repeated word is ambiguous and the
    # narrated lane asks which occurrence the creator means.
    occurrence = _fact_occurrence(req)
    return SpokenTextAsk(
        requirement_id=req.id,
        text=text,
        triggers=triggers,
        speaker=speaker,
        occurrence=occurrence,
        hold_s=spoken_text_hold_s(req),
    )


def _contains_run(tokens: tuple[str, ...], run: tuple[str, ...]) -> bool:
    return any(tokens[i : i + len(run)] == run for i in range(len(tokens) - len(run) + 1))


def narrowed_to_answer(req: BriefRequirement, value: object) -> BriefRequirement | None:
    """``req`` with the trigger the creator's occurrence answer was about put back (KRI-972).

    The question is asked about the creator's own trigger words ("grilled cheese"); its answer
    records them as ``value``. When the brief later carries those words inside a longer
    trigger ("Provolone grilled cheese", lifted from the option's sentence that came back as
    the tap's message), the pick would time the wrong word. The answered words replace that
    longer trigger. None when no trigger of ``req`` widens ``value`` (nothing to put back)."""
    from app.services.spoken_quotes import fold_tokens  # noqa: PLC0415

    want = tuple(fold_tokens(str(value or "")))
    if not want:
        return None
    for key in SPOKEN_TEXT_CUE_KEYS:
        raw = req.facts.get(key)
        items = list(raw) if isinstance(raw, list | tuple) else [raw]
        for index, item in enumerate(items):
            if not isinstance(item, str):
                continue
            folded = tuple(fold_tokens(item))
            if folded != want and _contains_run(folded, want):
                items[index] = str(value).strip()
                kept = items if isinstance(raw, list | tuple) else items[0]
                return req.model_copy(update={"facts": {**req.facts, key: kept}})
    return None


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
    "MAX_HOLD_S",
    "MIN_HOLD_S",
    "SPOKEN_TEXT_CUE_KEYS",
    "SPOKEN_TEXT_ELEMENT_PREFIX",
    "SPOKEN_TEXT_HOLD_KEYS",
    "SPOKEN_TEXT_SPEAKER_KEY",
    "SpokenTextAsk",
    "brief_spoken_texts",
    "is_spoken_text_bar",
    "narrowed_to_answer",
    "requirement_spoken_text",
    "spoken_text_asks",
    "spoken_text_hold_s",
    "spoken_triggers",
    "with_answered_pins",
    "with_valid_hold_fact",
]
