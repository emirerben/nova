"""Cheap, deterministic sanity checks on transcripts the models hand back (KRI-653).

Speech models hallucinate on audio with nothing in it: Gemini returned "Hello." x1000
and Whisper returned "Bye! Bye! Bye!" for a talk-to-camera take whose microphone
recorded digital silence. A transcript like that must never count as speech.

Leaf module (stdlib only) so schemas, services and agents can all import it.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter

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
    if total < DEGENERATE_MIN_TOKENS:
        return False
    if total >= DEGENERATE_FEW_UNIQUE_MIN_TOKENS and len(set(tokens)) <= DEGENERATE_MAX_UNIQUE:
        return True
    for size in range(1, DEGENERATE_MAX_NGRAM + 1):
        grams = Counter(tuple(tokens[i : i + size]) for i in range(total - size + 1))
        _, top = grams.most_common(1)[0]
        if top * size / total >= DEGENERATE_NGRAM_COVERAGE:
            return True
    return False
