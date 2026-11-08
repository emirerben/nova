"""A production slate at the very start of a take ("okay, we're recording", "tamam, kayıttayız").

Speech cleanup cuts pauses, filler sounds and abandoned takes; a creator who says "okay, we're
recording" before the first real sentence restarts nothing, so none of those catch it (Çiğdem S2
"ss2b", 2026-11-08). This module finds that one line, deterministically, and hands it to the cut
planner as a retake-style word span, so it gets the same outward snapping, voice-guard exemption
and receipts as an abandoned take.

Precision over recall (failure modes: ``docs/reviews/cigdem-s2-failure-modes.md`` A1-A12):

* only the take's FIRST utterance, starting at word 0 (after optional fillers);
* the whole utterance, minus leading/trailing fillers, must equal a phrase on a closed list of
  recording / rolling / countdown / mic-check talk; a longer sentence never matches, and generic
  hooks ("ready?", "here we go", "action", "let's go") are not on the list;
* the utterance must END there: sentence-final punctuation on its last word, a gap of
  ``_MIN_GAP_S`` before the next word, or a detected silence starting at its end (Whisper word
  stamps can stretch over a pause); no boundary within ``_MAX_SLATE_WORDS`` words = no cut;
* it ends within ``_MAX_SLATE_S`` and leaves at least ``_MIN_WORDS_AFTER`` words after it.

Pure: no I/O, no settings. Callers gate it on ``SPEECH_CLEANUP_SLATE_CUT_ENABLED``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from app.text_fold import loose_text

_MAX_SLATE_WORDS = 6
_MAX_SLATE_S = 3.0
_MIN_GAP_S = 0.2
_MIN_SILENCE_S = 0.15
_MIN_WORDS_AFTER = 3

# Words that may sit around a slate without making it a real sentence ("okay, so, we're rolling").
_FILLERS = frozenset(
    {
        "okay",
        "ok",
        "okey",
        "alright",
        "all",  # "all right"
        "right",
        "so",
        "um",
        "uh",
        "erm",
        "hmm",
        "tamam",
        "evet",
        "hı",
        "ıı",
        "eee",
        "ee",
        "şey",
        "hadi",
        "peki",
    }
)

# Whole utterances (after fillers are stripped), lowercase, apostrophes removed.
_SLATE_PHRASES = frozenset(
    {
        # English: recording / rolling
        "were recording",
        "we are recording",
        "recording",
        "its recording",
        "it is recording",
        "is it recording",
        "are we recording",
        "is this recording",
        "recording now",
        "were recording now",
        "were rolling",
        "we are rolling",
        "rolling",
        "are we rolling",
        "camera rolling",
        "is this on",
        "is it on",
        "mic check",
        "sound check",
        "three two one",
        "3 2 1",
        "321",
        "in three two one",
        "lets start recording",
        "start recording",
        # Turkish: kayıt / çekim
        "kayıttayız",
        "kayıttayım",
        "kayıt başladı",
        "kayıt başlıyor",
        "kayıt",
        "kaydediyor",
        "kaydediyor mu",
        "kaydediyor muyuz",
        "kayıtta mı",
        "kayıtta mıyız",
        "çekiyor",
        "çekiyor mu",
        "çekiyoruz",
        "çekime başlıyoruz",
        "çekim başladı",
        "üç iki bir",
        "3 2 1",
        "ses kontrol",
        "ses kontrolü",
        "mikrofon kontrol",
    }
)

_SENTENCE_END = re.compile(r"[.!?…]+[\"'”’)]*$")
_STRIP = re.compile(r"[^\w\s]")


def _fold(text: str) -> str:
    """The shared loose fold (Turkish-aware case, diacritics stripped: "KAYITTAYIZ" ==
    "kayıttayız" == "kayittayiz"), apostrophes and punctuation removed."""
    text = loose_text(text).replace("'", "").replace("’", "")
    return " ".join(_STRIP.sub(" ", text).split())


# Match keys: the lists folded the way a transcript word is.
_FILLER_KEYS = frozenset(_fold(word) for word in _FILLERS)
_SLATE_KEYS = frozenset(_fold(phrase) for phrase in _SLATE_PHRASES)


def _value(raw: Any, *names: str) -> Any:
    for name in names:
        if isinstance(raw, Mapping):
            if name in raw:
                return raw[name]
        elif hasattr(raw, name):
            return getattr(raw, name)
    return None


def _words(words: Sequence[Any]) -> list[tuple[str, float, float]] | None:
    out: list[tuple[str, float, float]] = []
    for raw in words:
        text = _value(raw, "text", "word")
        start = _value(raw, "start_s", "start")
        end = _value(raw, "end_s", "end")
        if not isinstance(text, str) or not text.strip():
            return None
        try:
            lo, hi = float(start), float(end)
        except (TypeError, ValueError):
            return None
        if hi < lo:
            return None
        out.append((text.strip(), lo, hi))
    return out


def _ends_utterance(
    words: list[tuple[str, float, float]],
    j: int,
    silences: Sequence[tuple[float, float]],
) -> bool:
    text, start, end = words[j]
    if _SENTENCE_END.search(text):
        return True
    next_start = words[j + 1][1]
    if next_start - end >= _MIN_GAP_S:
        return True
    # Whisper stamps can stretch over a pause (onto this word's end or the next word's start), so
    # a silence that starts after this word began and before the next word's speech is the
    # boundary too. One that starts later belongs to a later word (ss2b: the pause after
    # "recording" must not end the utterance at "we're").
    return any(
        hi - lo >= _MIN_SILENCE_S and start + 0.05 <= lo <= next_start + 0.15 for lo, hi in silences
    )


def _is_slate(tokens: list[str]) -> bool:
    folded = [_fold(token) for token in tokens]
    folded = [token for token in folded if token]
    while folded and folded[0] in _FILLER_KEYS:
        folded.pop(0)
    while folded and folded[-1] in _FILLER_KEYS:
        folded.pop()
    return bool(folded) and " ".join(folded) in _SLATE_KEYS


def opening_slate_span(
    words: Sequence[Any],
    *,
    silences: Sequence[tuple[float, float]] = (),
) -> tuple[int, int] | None:
    """The inclusive word-index span of a production slate opening the take, else None.

    ``words`` are the take's timed words (``text``/``word``, ``start_s``/``start``,
    ``end_s``/``end``, as dicts or objects); ``silences`` detected silence spans in seconds.
    """
    parsed = _words(words)
    if not parsed or len(parsed) < 1 + _MIN_WORDS_AFTER:
        return None
    clean_silences: list[tuple[float, float]] = []
    for span in silences or ():
        try:
            lo, hi = float(span[0]), float(span[1])
        except (TypeError, ValueError, IndexError):
            continue
        if hi > lo:
            clean_silences.append((lo, hi))
    for j in range(min(_MAX_SLATE_WORDS, len(parsed) - 1)):
        if parsed[j][2] > _MAX_SLATE_S:
            return None
        if not _ends_utterance(parsed, j, clean_silences):
            continue
        # The first utterance ends at j: it is the slate or nothing is.
        if len(parsed) - (j + 1) < _MIN_WORDS_AFTER:
            return None
        return (0, j) if _is_slate([text for text, _s, _e in parsed[: j + 1]]) else None
    return None
