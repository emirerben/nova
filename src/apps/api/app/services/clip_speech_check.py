"""Are a clip's timed words the words actually said? (KRI-808)

whisper-1 picks ONE language per clip from its own audio. On a 2-4 s clip of accented or
mixed speech that guess is close to random (Don Julio M1: a Turkish speaker's "What's your
name?" came back as Turkish "Ne isminiz?", "Gracias" as Russian "Спасибо."), and whisper
then writes a TRANSLATION into the guessed language instead of the spoken words. One
language for the whole take is no better: the same 26 s take holds English, an Argentine
waiter's Spanish and Turkish table chatter, and whole-take auto-detect says Turkish.

So the language is decided per clip, from evidence, and never forced:

* Gemini's transcript of the same clip is an independent listener. When whisper's words
  and a believable Gemini transcript clearly disagree (a different EN/TR language, another
  script, or not one word in common), whisper's pass is suspect.
* A suspect pass is transcribed again in the language Gemini heard and in the chat's
  language, and a re-run is kept only when most of its words are words Gemini heard too.
  Forcing a language onto speech in another one makes whisper translate or print a
  placeholder, which then shares nothing with Gemini, so it is never kept.
* A suspect pass that nothing confirms is dropped: no timed words beats wrong ones.

``clean_words`` then drops what whisper writes over silence: bracketed placeholders,
caption credits, a sentence looped three or more times, and more words than the clip holds.

Pure functions; the caller supplies the re-transcribe callable.
"""

from __future__ import annotations

import unicodedata
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.pipeline.caption_language import crosscheck_detected_language
from app.services.speech_segments import sentence_groups
from app.services.transcript_quality import (
    exceeds_speech_rate,
    is_degenerate_transcript,
    speech_tokens,
)

# A re-run is kept only when at least this share of its words are words Gemini heard.
MIN_HEARD_SHARE = 0.5
# A sentence said this many times in a row is a decoder loop: only the first is kept.
LOOP_REPEATS = 3
# whisper's text for "not speech" and its silence-time caption credits, folded.
_CREDIT_PHRASES: tuple[tuple[str, ...], ...] = (
    ("altyazi", "m", "k"),
    ("amara", "org"),
    ("subtitles", "by"),
    ("sous", "titres"),
    ("untertitel", "im", "auftrag"),
)
_BRACKETS = {"(": ")", "[": "]", "♪": "♪", "*": "*"}
_EDGE = " \t\"'“”‘’«»-–—.,!?¡¿…:;"

Retranscribe = Callable[[str], tuple[list[dict[str, Any]], str]]


@dataclass(frozen=True)
class SpokenWords:
    """The words to store for a clip, and why."""

    words: list[dict[str, Any]] = field(default_factory=list)
    language: str = ""
    # "kept" | "retranscribed" | "dropped"
    outcome: str = "kept"
    reason: str = ""
    tried: tuple[str, ...] = ()


def _text(words: Sequence[Any]) -> str:
    return " ".join(
        str((w.get("text") if isinstance(w, dict) else getattr(w, "text", "")) or "").strip()
        for w in words or []
    ).strip()


def dominant_script(text: str | None) -> str:
    """The Unicode script most letters of ``text`` are in ("LATIN", "CYRILLIC", ...)."""
    counts: Counter[str] = Counter()
    for ch in str(text or ""):
        if not ch.isalpha():
            continue
        try:
            counts[unicodedata.name(ch).split(" ", 1)[0]] += 1
        except ValueError:
            continue
    return counts.most_common(1)[0][0] if counts else ""


def heard_share(text: str | None, reference: str | None) -> float:
    """Share of ``text``'s words that ``reference`` also has (0.0 when ``text`` is empty)."""
    tokens = speech_tokens(text)
    if not tokens:
        return 0.0
    heard = set(speech_tokens(reference))
    return sum(1 for token in tokens if token in heard) / len(tokens)


def suspect_reason(text: str, detected: str | None, reference: str | None) -> str | None:
    """Why whisper's ``text`` is probably not what was said, or None when nothing says so."""
    if not str(text or "").strip() or not str(reference or "").strip():
        return None
    if crosscheck_detected_language(detected, reference_text=reference) is not None:
        return "language"
    ours, theirs = dominant_script(text), dominant_script(reference)
    if ours and theirs and ours != theirs:
        return "script"
    if heard_share(text, reference) == 0.0:
        return "no_shared_words"
    return None


def _same_language(a: str | None, b: str | None) -> bool:
    return bool(a) and str(a).strip().lower() == str(b or "").strip().lower()


def pick_spoken_words(
    words: list[dict[str, Any]],
    detected: str,
    *,
    reference: str | None,
    candidates: Callable[[], Sequence[str]],
    retranscribe: Retranscribe,
    duration_s: float | None = None,
) -> SpokenWords:
    """Keep whisper's first pass, replace it with a re-run Gemini confirms, or drop it.

    ``candidates`` is called only for a suspect pass (it may read the chat's language).
    ``retranscribe(language)`` returns ``(words, language)``; it may raise, and a raise
    only skips that language.
    """
    words = clean_words(words, duration_s=duration_s)
    reason = suspect_reason(_text(words), detected, reference)
    if reason is None:
        return SpokenWords(words=words, language=detected, outcome="kept")
    first_share = heard_share(_text(words), reference)
    heard = crosscheck_detected_language(detected, reference_text=reference)
    tried: list[str] = []
    best: tuple[float, list[dict[str, Any]], str] | None = None
    for language in [heard, *candidates()]:
        code = str(language or "").strip().lower()
        if not code or code in tried or _same_language(code, detected):
            continue
        tried.append(code)
        try:
            again, _reported = retranscribe(code)
        except Exception:  # noqa: BLE001 - a failed re-run only skips this language
            continue
        again = clean_words(again, duration_s=duration_s)
        share = heard_share(_text(again), reference)
        if share >= MIN_HEARD_SHARE and share > first_share and (best is None or share > best[0]):
            best = (share, again, code)
    if best is not None:
        return SpokenWords(
            words=best[1],
            language=best[2],
            outcome="retranscribed",
            reason=reason,
            tried=tuple(tried),
        )
    return SpokenWords(outcome="dropped", reason=reason, tried=tuple(tried))


def _unbracketed(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``rows`` without whisper's bracketed non-speech: "(speaking in foreign language)",
    "[Música]", "(laughing)", a "♪" line. Words outside the brackets stay."""
    out: list[dict[str, Any]] = []
    closing = ""
    pending: list[dict[str, Any]] = []
    for row in rows:
        text = str(row.get("text") or "").strip().strip(_EDGE)
        if closing:
            pending.append(row)
            if text.endswith(closing):
                closing, pending = "", []
            continue
        close = _BRACKETS.get(text[:1])
        if close is not None:
            if not (len(text) > 1 and text.endswith(close)):
                closing, pending = close, [row]
            continue
        out.append(row)
    return out + pending  # a bracket that never closes was not a marker


def _credit(tokens: list[str]) -> bool:
    width = len(tokens)
    return any(
        tokens[i : i + len(phrase)] == list(phrase)
        for phrase in _CREDIT_PHRASES
        for i in range(width - len(phrase) + 1)
    )


def clean_words(words: Sequence[Any], *, duration_s: float | None = None) -> list[dict[str, Any]]:
    """``words`` without whisper's silence-time inventions; [] when the whole pass is one.

    Judged per sentence, grouped exactly as ``words_to_segments`` groups them.
    """
    rows = _unbracketed([w if isinstance(w, dict) else _as_dict(w) for w in words or []])
    groups = sentence_groups(rows)
    keys = [tuple(speech_tokens(_text(group))) for group in groups]
    out: list[dict[str, Any]] = []
    index = 0
    while index < len(groups):
        # A run of the same sentence: LOOP_REPEATS or more in a row keeps only the first.
        end = index + 1
        while end < len(groups) and keys[end] == keys[index]:
            end += 1
        tokens = list(keys[index])
        if tokens and not _credit(tokens):
            for group in groups[index : end if end - index < LOOP_REPEATS else index + 1]:
                if not is_degenerate_transcript(_text(group)):
                    out.extend(group)
        index = end
    if exceeds_speech_rate(_text(out), duration_s):
        return []
    return out


def _f(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _as_dict(word: Any) -> dict[str, Any]:
    return {
        "text": getattr(word, "text", ""),
        "start_s": getattr(word, "start_s", 0.0),
        "end_s": getattr(word, "end_s", 0.0),
    }


__all__ = [
    "LOOP_REPEATS",
    "MIN_HEARD_SHARE",
    "SpokenWords",
    "clean_words",
    "dominant_script",
    "heard_share",
    "pick_spoken_words",
    "suspect_reason",
]
