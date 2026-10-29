"""On-screen text shown in sequence is not captions (29 Ekim lane D, KRI-1038).

"Show each word one by one ... and say “<sentence>”" asks for the creator's own words to
appear on screen a word at a time. The brief filed it as ``style`` ``captions: word_by_word``,
the speech lane, dropped the quote, and the planner then offered to record a voiceover for it.

The brief now has its own kind::

    {"kind": "text_sequence", "scope": "title", "literal": "<the quoted words, verbatim>",
     "facts": {"unit": "word" | "phrase", "mode": "replace" | "cumulative" | "agent_choice"}}

The model records ``mode`` when the creator made the call ("keep them on screen" builds up,
"each word replaces the last"); otherwise ``agent_choice``: the editor agent picks and names its
choice in the reply so the creator can flip it.

The Main Creator prompt teaches the shape. `refile_text_sequence` is the backstop for a reply
that still files the ask as ``captions: word_by_word`` (the model under the older prompt, a
cached cassette). The model's own fact says what the ask is; the server hands it BACK to the
speech lane (leaves the model's filing alone) when the creator named captions
(`text_asks_for_captions`), the item already has a recorded voiceover, or the message talks
about words said in the footage (`names_footage_speech`: "when I say", "dediğim yerde"). A bare
"and say “...”" is not that: with no voiceover it is on-screen text. Everything else is
returned exactly as the model wrote it.
"""

from __future__ import annotations

import unicodedata
from typing import TYPE_CHECKING, Any, Literal

from app.kria.brief_route import loose_text
from app.pipeline.song_captions import text_asks_for_captions
from app.services.spoken_quotes import MIN_QUOTE_TOKENS, quoted_texts

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from app.kria.brief import BriefUpdate

TEXT_SEQUENCE_KIND = "text_sequence"
UNIT_FACT = "unit"
MODE_FACT = "mode"
TextSequenceUnit = Literal["word", "phrase"]
TextSequenceMode = Literal["replace", "cumulative", "agent_choice"]

# The brief literal is capped at 200 characters; a longer quote cannot be stored verbatim, so
# it is left to the model's own filing rather than cut.
_MAX_LITERAL = 200
_FILED_AS = frozenset({"style", "text"})
_CAPTION_FACTS = ("captions", "caption_language")

# A quote is something SAID in the footage, not a title to show, when the message puts a speaker
# or a spoken tense on the speech verb ("when I say", "Deniz says", "as they are spoken",
# "dediğim yerde"). A bare imperative ("and say “...”", "I meant say to mean show") is the
# creator talking to the editor, so it is not on this list. The bare forms below count only
# right after a speaker. Folded words; Turkish by stem.
_SPOKEN_FORMS = frozenset(
    {
        "says", "said", "saying", "speaks", "speaking", "spoke", "spoken", "talks", "talking",
        "talked", "tells", "telling", "told", "sings", "singing", "sang", "sung", "shouts",
        "shouting", "shouted",
    }
)  # fmt: skip
_BARE_SPEECH = frozenset({"say", "speak", "talk", "tell", "sing", "shout"})
_SPEAKERS = frozenset({"i", "we", "he", "she", "they", "someone", "everyone", "anyone"})
_TR_SPOKEN_STEMS = ("dedi", "diyor", "derken", "deyince", "soyledi", "soylerken", "konus", "bagir")


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text).strip()


def _words(text: str) -> list[str]:
    return "".join(ch if ch.isalnum() else " " for ch in loose_text(text)).split()


def names_footage_speech(text: str) -> bool:
    """The message talks about words SAID in the footage (a spoken tense, or a speaker on the
    verb), so a quote in it is speech and never a title to show."""
    words = _words(text)
    for i, word in enumerate(words):
        if word in _SPOKEN_FORMS or word.startswith(_TR_SPOKEN_STEMS):
            return True
        if word in _BARE_SPEECH and i > 0 and words[i - 1] in _SPEAKERS:
            return True
    return False


def is_text_sequence(req: object) -> bool:
    return getattr(req, "kind", None) == TEXT_SEQUENCE_KIND


def text_sequence_unit(req: object) -> TextSequenceUnit:
    value = str((getattr(req, "facts", None) or {}).get(UNIT_FACT) or "").strip().casefold()
    return "phrase" if value == "phrase" else "word"


def text_sequence_mode(req: object) -> TextSequenceMode:
    value = str((getattr(req, "facts", None) or {}).get(MODE_FACT) or "").strip().casefold()
    return value if value in {"replace", "cumulative"} else "agent_choice"  # type: ignore[return-value]


def _quoted_literal(message: str) -> str | None:
    """The longest quoted run of at least ``MIN_QUOTE_TOKENS`` words in ``message``."""
    runs = [
        _nfc(run)
        for run in quoted_texts(message)
        if len(_words(run)) >= MIN_QUOTE_TOKENS and len(_nfc(run)) <= _MAX_LITERAL
    ]
    return max(runs, key=lambda run: len(_words(run))) if runs else None


def last_quoted_literal(texts: Iterable[str]) -> str | None:
    """The newest quoted sentence among ``texts`` (newest last), for a follow-up that points
    back at the words ("show each word after the other in the text"). A quote in a message that
    talks about speech in the footage is a spoken line, so it is never offered as a title."""
    for text in reversed(list(texts)):
        if names_footage_speech(text):
            continue
        literal = _quoted_literal(text)
        if literal:
            return literal
    return None


def _caption_fact(facts: Mapping[str, Any]) -> str | None:
    value = facts.get("captions")
    if isinstance(value, bool):
        return "on" if value else "off"
    if not isinstance(value, str):
        return None
    return "_".join(value.strip().casefold().replace("-", " ").split())


def refile_text_sequence(
    update: BriefUpdate,
    *,
    creator_message: str,
    known_literal: str | None = None,
    has_voiceover: bool = False,
) -> BriefUpdate:
    """A word-by-word captions filing of words the creator wants SHOWN, as a ``text_sequence``.

    Re-filed only when all hold: the update is an add / change filed as ``style`` or ``text``
    with the model's own ``captions: word_by_word`` fact; the creator did not name captions
    and the item has no recorded voiceover (``has_voiceover``); the message does not talk about
    words said in the footage (`names_footage_speech`: "when I say", "dediğim yerde"); and the
    words themselves are known, either quoted in the message (at least two words, 200 characters
    at most) or ``known_literal`` from earlier in the thread. ``unit`` is ``word`` and ``mode`` is
    ``agent_choice``: the editor agent reads the wording for the rest. Anything else is returned
    exactly as the model wrote it.
    """
    if update.operation == "remove" or update.kind not in _FILED_AS:
        return update
    facts = dict(update.facts or {})
    if (
        _caption_fact(facts) != "word_by_word"
        or has_voiceover
        or text_asks_for_captions(creator_message)
        or names_footage_speech(creator_message)
    ):
        return update
    literal = _quoted_literal(creator_message) or (
        _nfc(known_literal) if known_literal and len(_nfc(known_literal)) <= _MAX_LITERAL else None
    )
    if not literal:
        return update
    kept = {k: v for k, v in facts.items() if k not in _CAPTION_FACTS}
    return update.model_copy(
        update={
            "kind": TEXT_SEQUENCE_KIND,
            "scope": "title",
            "literal": literal,
            "facts": {**kept, UNIT_FACT: "word", MODE_FACT: "agent_choice"},
        }
    )


def refile_text_sequences(
    updates: Sequence[BriefUpdate],
    *,
    creator_message: str,
    known_literal: str | None = None,
    has_voiceover: bool = False,
) -> list[BriefUpdate]:
    return [
        refile_text_sequence(
            u,
            creator_message=creator_message,
            known_literal=known_literal,
            has_voiceover=has_voiceover,
        )
        for u in updates
    ]


__all__ = [
    "MODE_FACT",
    "TEXT_SEQUENCE_KIND",
    "UNIT_FACT",
    "is_text_sequence",
    "last_quoted_literal",
    "names_footage_speech",
    "refile_text_sequence",
    "refile_text_sequences",
    "text_sequence_mode",
    "text_sequence_unit",
]
