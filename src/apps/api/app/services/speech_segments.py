"""Timed speech for spoken-excerpt montages (KRI-282).

Three small, pure pieces plus one I/O helper:

* ``words_to_segments`` -- whisper words -> timed sentence segments (what the
  planner reads and quotes).
* ``ground_excerpt`` -- a quoted phrase -> the exact word-timed window to play,
  padded so the excerpt starts and ends on silence, never mid-word and never
  inside a neighbouring sentence. This is the same "planner asks by phrase,
  worker grounds to word timings at render time" pattern as the KRI-178
  reaction beats (``phone_reaction_grounding``), and it reuses that module's
  token folding so accents, case, punctuation and number words match the same
  way.
* ``transcribe_clip`` -- the cached whisper path (``transcribe_whisper_cached``)
  on a downloaded analysis proxy.

Nothing here changes a render unless ``settings.speech_excerpt_montage_enabled``
is on and a caller opts in.
"""

from __future__ import annotations

import re
import tempfile
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import structlog

from app.schemas.clip_understanding import SpeechSegment
from app.services.phone_reaction_grounding import _find_all, _token_stream, fold_tokens

log = structlog.get_logger()

# A sentence ends at terminal punctuation, a long pause, or these hard caps.
_SENTENCE_END = re.compile(r"[.!?…]+[\"')\]]*$")
_PAUSE_BREAK_S = 0.8
_MAX_SEGMENT_S = 14.0
_MAX_SEGMENT_WORDS = 45
# Fewer spoken words than this is not a "meaningful excerpt" source.
MIN_SPEECH_WORDS = 6

# Excerpt padding. The lead is short so the previous word never bleeds in; the
# tail is longer so the final consonant and room decay are not clipped.
EXCERPT_LEAD_S = 0.06
EXCERPT_TAIL_S = 0.22
# Never pad into a neighbouring word: take at most this share of the silent gap
# on each side, so the windows of two adjacent excerpts can never overlap.
_GAP_SHARE = 0.45
# A fuzzy (non-verbatim) quote must cover this share of its tokens.
FUZZY_MIN_OVERLAP = 0.8
_ELLIPSIS = re.compile(r"\s*(?:\.{3}|…)\s*")


def _word_dict(word: Any) -> dict[str, Any]:
    if isinstance(word, dict):
        return word
    return {
        "text": getattr(word, "text", ""),
        "start_s": getattr(word, "start_s", 0.0),
        "end_s": getattr(word, "end_s", 0.0),
    }


def words_to_segments(words: Sequence[Any]) -> list[SpeechSegment]:
    """Group timed words into sentence segments (source-clip seconds)."""
    rows = [_word_dict(w) for w in words or []]
    segments: list[SpeechSegment] = []
    bucket: list[dict[str, Any]] = []

    def flush() -> None:
        if not bucket:
            return
        text = " ".join(str(w.get("text") or "").strip() for w in bucket).strip()
        start = float(bucket[0].get("start_s") or 0.0)
        end = float(bucket[-1].get("end_s") or start)
        if text and end > start:
            segments.append(SpeechSegment(start_s=round(start, 2), end_s=round(end, 2), text=text))
        bucket.clear()

    for index, word in enumerate(rows):
        text = str(word.get("text") or "").strip()
        if not text:
            continue
        bucket.append(word)
        end = float(word.get("end_s") or 0.0)
        nxt = rows[index + 1] if index + 1 < len(rows) else None
        gap = (float(nxt.get("start_s") or 0.0) - end) if nxt is not None else 99.0
        span = end - float(bucket[0].get("start_s") or 0.0)
        if (
            _SENTENCE_END.search(text)
            or gap >= _PAUSE_BREAK_S
            or span >= _MAX_SEGMENT_S
            or len(bucket) >= _MAX_SEGMENT_WORDS
        ):
            flush()
    flush()
    return segments


def spoken_word_count(words: Sequence[Any]) -> int:
    return sum(1 for w in words or [] if str(_word_dict(w).get("text") or "").strip())


@dataclass(frozen=True)
class GroundedExcerpt:
    """A quoted phrase resolved to a playable, sentence-safe source window."""

    start_s: float
    end_s: float
    # First/last spoken word of the match (unpadded), for receipts and tests.
    word_start_s: float
    word_end_s: float
    exact: bool
    overlap: float


def _safe_window(
    stream: list[Any], first: int, last: int, duration_s: float | None
) -> tuple[float, float]:
    word_start = stream[first].start_s
    word_end = stream[last].end_s
    start = word_start - EXCERPT_LEAD_S
    end = word_end + EXCERPT_TAIL_S
    # Walk to the nearest token that is a genuinely different word (the stream
    # may hold several tokens per word, all sharing one timing).
    prev_end = next(
        (stream[i].end_s for i in range(first - 1, -1, -1) if stream[i].end_s <= word_start + 1e-6),
        None,
    )
    next_start = next(
        (
            stream[i].start_s
            for i in range(last + 1, len(stream))
            if stream[i].start_s >= word_end - 1e-6
        ),
        None,
    )
    if prev_end is not None:
        start = max(start, word_start - (word_start - prev_end) * _GAP_SHARE)
    if next_start is not None:
        end = min(end, word_end + (next_start - word_end) * _GAP_SHARE)
    start = max(0.0, min(start, word_start))
    end = max(end, word_end)
    if duration_s is not None:
        end = min(end, duration_s)
    return round(start, 3), round(end, 3)


def _exact_span(tokens: list[str], target: list[str], after: int = 0) -> tuple[int, int] | None:
    starts = [i for i in _find_all(tokens, target) if i >= after]
    if not starts:
        return None
    return starts[0], starts[0] + len(target) - 1


def _fuzzy_span(tokens: list[str], target: list[str]) -> tuple[int, int, float] | None:
    """Best contiguous window whose token multiset overlaps the quote enough."""
    if len(target) < 3 or not tokens:
        return None
    want = Counter(target)
    best: tuple[float, int, int] | None = None
    low = max(2, int(len(target) * 0.8))
    high = int(len(target) * 1.25) + 1
    for width in range(low, min(high, len(tokens)) + 1):
        for start in range(0, len(tokens) - width + 1):
            have = Counter(tokens[start : start + width])
            hit = sum((want & have).values())
            score = hit / len(target)
            if score >= FUZZY_MIN_OVERLAP and (best is None or score > best[0] + 1e-9):
                best = (score, start, start + width - 1)
    if best is None:
        return None
    score, first, last = best
    # Trim to the first/last token that is actually part of the quote.
    while first < last and tokens[first] not in want:
        first += 1
    while last > first and tokens[last] not in want:
        last -= 1
    return first, last, score


def ground_excerpt(
    words: Sequence[Any],
    quote: str,
    *,
    duration_s: float | None = None,
    min_after_s: float = 0.0,
) -> GroundedExcerpt | None:
    """Resolve ``quote`` against word timings, or ``None`` when it was not said.

    A quote may be a verbatim phrase, or ``"first words ... last words"`` for a
    long passage (the window spans from the head to the tail). A non-verbatim
    quote falls back to the best contiguous window that covers at least
    ``FUZZY_MIN_OVERLAP`` of its tokens, because whisper and the planner's
    reading of a transcript legitimately differ by a word or two. A quote that
    matches nothing is never guessed.

    ``min_after_s`` skips earlier occurrences, so the same phrase quoted twice
    resolves to the first and then the next time it is said.
    """
    stream = _token_stream([_word_dict(w) for w in words or []])
    if not stream or not str(quote or "").strip():
        return None
    tokens = [t.text for t in stream]
    first_index = next((i for i, t in enumerate(stream) if t.start_s >= min_after_s - 1e-6), None)
    if first_index is None:
        return None

    parts = [p for p in _ELLIPSIS.split(str(quote).strip()) if p.strip()]
    spans: list[tuple[int, int]] = []
    exact = True
    overlap = 1.0
    cursor = first_index
    for part in parts:
        target = fold_tokens(part)
        if not target:
            continue
        span = _exact_span(tokens, target, after=cursor)
        if span is None:
            fuzzy = _fuzzy_span(tokens[cursor:], target)
            if fuzzy is None:
                return None
            span = (fuzzy[0] + cursor, fuzzy[1] + cursor)
            exact = False
            overlap = min(overlap, fuzzy[2])
        spans.append(span)
        cursor = span[1] + 1
    if not spans:
        return None
    first, last = spans[0][0], spans[-1][1]
    start, end = _safe_window(stream, first, last, duration_s)
    return GroundedExcerpt(
        start_s=start,
        end_s=end,
        word_start_s=stream[first].start_s,
        word_end_s=stream[last].end_s,
        exact=exact,
        overlap=round(overlap, 3),
    )


def transcribe_clip(
    local_path: str, *, language: str | None = None
) -> tuple[list[dict[str, Any]], str]:
    """Word-timed transcript of one local media file via the cached whisper path."""
    from app.pipeline.transcribe import transcribe_whisper_cached  # noqa: PLC0415

    transcript = transcribe_whisper_cached(local_path, language=language)
    words = [
        {
            "text": w.text,
            "start_s": float(w.start_s),
            "end_s": float(w.end_s),
            "confidence": float(w.confidence),
        }
        for w in transcript.words
    ]
    return words, str(getattr(transcript, "language", "") or "")


def transcribe_stored_clip(
    gcs_path: str,
    *,
    download: Callable[[str, str], Any] | None = None,
    transcribe: Callable[[str], tuple[list[dict[str, Any]], str]] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Download one stored clip (an analysis proxy) and transcribe it. May raise."""
    if download is None:
        from app.storage import download_to_file  # noqa: PLC0415

        download = download_to_file
    run = transcribe or transcribe_clip
    with tempfile.TemporaryDirectory(prefix="speech-seg-") as tmp:
        local = f"{tmp}/clip.mp4"
        download(gcs_path, local)
        return run(local)


def attach_segments_to_analysis(
    analysis: dict[str, Any], words: Sequence[Any], language: str = ""
) -> dict[str, Any]:
    """A copy of ``analysis`` with timed segments in ``understanding.speech``.

    Only ever adds ``segments``/``language``; the rest of the stored record is
    untouched. Returns the input unchanged when there is nothing to attach.
    """
    segments = words_to_segments(words)
    if not segments:
        return analysis
    out = dict(analysis)
    block = dict(out.get("understanding") or {})
    speech = dict(block.get("speech") or {})
    speech["segments"] = [seg.model_dump() for seg in segments]
    if language:
        speech["language"] = language
    speech.setdefault("has_speech", True)
    block["speech"] = speech
    out["understanding"] = block
    return out


__all__ = [
    "EXCERPT_LEAD_S",
    "EXCERPT_TAIL_S",
    "FUZZY_MIN_OVERLAP",
    "MIN_SPEECH_WORDS",
    "GroundedExcerpt",
    "attach_segments_to_analysis",
    "ground_excerpt",
    "spoken_word_count",
    "transcribe_clip",
    "transcribe_stored_clip",
    "words_to_segments",
]
