"""Cheap, deterministic sanity checks on transcripts the models hand back (KRI-653).

Speech models hallucinate on audio with nothing in it: Gemini returned "Hello." x1000
and Whisper returned "Bye! Bye! Bye!" for a talk-to-camera take whose microphone
recorded digital silence. A transcript like that must never count as speech.

Leaf module (stdlib and the stdlib-only ``app.text_fold``) so schemas, services and agents
can all import it.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter

from app.text_fold import loose_case

# Letters only (Unicode-aware), with an inner apostrophe kept so "don't" stays one token.
_TOKEN_RE = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)?")


def _fold(text: str) -> str:
    """Case-fold and drop combining marks, so Turkish "İstanbul" is one token, not "i" + "stanbul".

    Folding ş/ğ/é to s/g/e can make two different words collide; that only matters for
    *repetition* counting, where a collision is harmless.
    """
    decomposed = unicodedata.normalize("NFD", text.casefold())
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")


# A real talk has no single 1-3 word phrase covering most of it. Repetition this
# dominant over this many words is a decoder loop, not a speaker.
DEGENERATE_MIN_TOKENS = 20
DEGENERATE_NGRAM_COVERAGE = 0.6
DEGENERATE_MAX_NGRAM = 3
# Many words but almost no vocabulary ("Hello" x 600 is caught above; this catches
# alternating loops like "yes no yes no ...").
DEGENERATE_FEW_UNIQUE_MIN_TOKENS = 30
DEGENERATE_MAX_UNIQUE = 3
# KRI-808: a short clip loops in fewer words. Gemini wrote "Video, yeah. Video. Video. ..."
# (12 tokens, 11 of them "video") for 2 s of quiet table chatter. Below the 20-token floor
# one phrase must cover even more of the text, so "no no no no no" (5) and a real
# "Lucas. Lucas, yeah." stay speech.
SHORT_LOOP_MIN_TOKENS = 8
SHORT_LOOP_COVERAGE = 0.75
# KRI-808: more words than the clip can hold is invented speech. Gemini wrote 24 words for
# a 1.1 s clip. Fast real speech is about 4 words a second; the slack keeps a short clip's
# "Thank you so much" (4 words in 0.6 s) speech.
MAX_WORDS_PER_SECOND = 5.0
RATE_SLACK_WORDS = 4


def _coverage(tokens: list[str]) -> float:
    """The largest share of ``tokens`` that one repeated 1-3 word phrase covers."""
    total = len(tokens)
    best = 0.0
    for size in range(1, DEGENERATE_MAX_NGRAM + 1):
        grams = Counter(tuple(tokens[i : i + size]) for i in range(total - size + 1))
        if grams:
            _, top = grams.most_common(1)[0]
            best = max(best, top * size / total)
    return best


def is_degenerate_transcript(text: str | None) -> bool:
    """True when ``text`` is a repetition loop rather than something a person said.

    Deliberately blunt: a deliberate chant ("let's go " x 15) is also flagged. For a clip's
    *transcript* that is the right trade: nobody captions a chant from a model's loop, and the
    cost of a false positive is "no transcript", never a blocked render.
    """
    if not text or not isinstance(text, str):
        return False
    tokens = _TOKEN_RE.findall(_fold(text))
    total = len(tokens)
    if total < SHORT_LOOP_MIN_TOKENS:
        return False
    if total < DEGENERATE_MIN_TOKENS:
        return _coverage(tokens) >= SHORT_LOOP_COVERAGE
    if total >= DEGENERATE_FEW_UNIQUE_MIN_TOKENS and len(set(tokens)) <= DEGENERATE_MAX_UNIQUE:
        return True
    return _coverage(tokens) >= DEGENERATE_NGRAM_COVERAGE


def speech_tokens(text: str | None) -> list[str]:
    """Folded word tokens in any script, for comparing two transcripts of one clip.

    The shared Turkish-aware fold (KRI-781), so "mı" and "mi" compare equal.
    """
    if not isinstance(text, str):
        return []
    return _TOKEN_RE.findall(loose_case(text))


def word_count(text: str | None) -> int:
    return len(speech_tokens(text))


def exceeds_speech_rate(text: str | None, duration_s: object) -> bool:
    """True when ``text`` has more words than a clip of ``duration_s`` seconds can hold.

    An unknown or non-positive duration never flags anything.
    """
    try:
        duration = float(duration_s)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    if duration <= 0:
        return False
    return word_count(text) > MAX_WORDS_PER_SECOND * duration + RATE_SLACK_WORDS


def is_implausible_transcript(text: str | None, duration_s: object = None) -> bool:
    """A loop, or more words than the clip's length allows: not something anyone said."""
    return is_degenerate_transcript(text) or exceeds_speech_rate(text, duration_s)
