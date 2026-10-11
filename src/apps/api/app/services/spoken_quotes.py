"""A line the creator quotes from their own footage: which clip says it, and when (KRI-597).

"End on the moment I say 'Huzuru buldum ben burada'" names a spoken line, not a picture.
Two things went wrong with it in prod (thread 6605cf61): the clip resolver saw only the
first 200 transcript characters of each clip, so it seated the line on a clip that never
says it (with made-up evidence), and the clip that does say it had lost the line from its
timed whisper segments, so nothing knew *when* it is said.

Everything here is deterministic except ``ground_closing_quote``'s optional word-timed
whisper pass, which runs only when the stored segments miss a line the untimed transcript
has:

* ``spoken_quote`` -- the quoted words of an ask about something said (not a sign).
* ``regound_quoted_intents`` -- move a single-clip intent whose quote its clip never says
  to the one clip whose transcript does; anything ambiguous is left as the resolver had it.
* ``ground_closing_quote`` -- the source window of a quoted closing line, from the timed
  segments, else from word timings; ``status="not_found"`` when neither has it.
* ``is_outro_hallucination`` -- whisper's "thanks for watching" on trailing silence.
"""

from __future__ import annotations

import asyncio
import re
import tempfile
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import structlog

from app.kria.reply_language import say
from app.schemas.clip_intents import ResolvedClipIntent
from app.services.phone_reaction_grounding import _find_all, fold_tokens
from app.services.speech_segments import (
    EXCERPT_TAIL_S,
    _fuzzy_span,
    ground_excerpt,
    words_to_segments,
)

log = structlog.get_logger()

# A quoted run of words: straight or curly double quotes, guillemets, curly singles, or
# straight singles that are not an apostrophe ("I'll", "Tigre'de" stay inside the quote).
_QUOTED_RE = re.compile(
    r"\"([^\"\n]{2,200})\""
    r"|“([^”\n]{2,200})”"
    r"|„([^“”\n]{2,200})[“”]"
    r"|«([^»\n]{2,200})»"
    r"|‘([^’\n]{2,200})’"
    r"|(?<![\w'])'((?:[^'\n]|'(?=[^\W\d_]))+?)'(?!\w)"
)
MIN_QUOTE_TOKENS = 2
# The quote is something said, not a sign or a caption: "the moment I say '...'",
# "where Deniz says '...'", "'...' dediğim an". Folded words, and Turkish stems.
_SPEECH_CUES = frozenset(
    {
        "say",
        "says",
        "said",
        "saying",
        "speak",
        "speaks",
        "speaking",
        "spoke",
        "talk",
        "talks",
        "talking",
        "tell",
        "tells",
        "telling",
        "told",
        "shout",
        "shouts",
        "sing",
        "sings",
        "singing",
        "voice",
        "sentence",
    }
)
_SPEECH_STEMS = ("dedi", "diyor", "derken", "deyince", "soyle", "konus", "cumle", "bagir")

# A closing line found in timed segments keeps a breath either side, as KRI-517 does.
SEGMENT_LEAD_S = 0.15
SEGMENT_TAIL_S = 0.3
# A quote that is part of a longer sentence ends with that sentence when it is close.
SENTENCE_REACH_S = 2.5
# The longest run of timed segments one quote may span.
_MAX_SEGMENT_RUN = 3
WHISPER_TIMEOUT_S = 45.0

# whisper-1's trailing-silence inventions, folded: never a line anyone said.
_OUTRO_HALLUCINATIONS = (
    ("izlediginiz", "icin", "tesekkur", "ederim"),
    ("izlediginiz", "icin", "tesekkurler"),
    ("abone", "olmayi", "unutmayin"),
    ("thanks", "for", "watching"),
    ("thank", "you", "for", "watching"),
    ("please", "subscribe"),
)

Transcribe = Callable[..., tuple[list[dict[str, Any]], str]]


def quoted_phrases(text: str | None) -> list[str]:
    """Quoted runs of at least two words, in the order they appear."""
    out: list[str] = []
    for match in _QUOTED_RE.finditer(str(text or "")):
        phrase = next(group for group in match.groups() if group is not None).strip()
        if len(fold_tokens(phrase)) >= MIN_QUOTE_TOKENS:
            out.append(phrase)
    return out


def spoken_quote(text: str | None) -> str | None:
    """The first quoted phrase of ``text`` when ``text`` asks for something said."""
    quotes = quoted_phrases(text)
    if not quotes:
        return None
    rest = _QUOTED_RE.sub(" ", str(text or ""))
    cues = fold_tokens(rest)
    if any(cue in _SPEECH_CUES or cue.startswith(_SPEECH_STEMS) for cue in cues):
        return quotes[0]
    return None


def _says(tokens: list[str], target: list[str]) -> bool:
    if not target or not tokens:
        return False
    if _find_all(tokens, target):
        return True
    # Creators quote from memory: "ben burada" for "burada ben ya".
    return len(target) >= 3 and _fuzzy_span(tokens, target) is not None


def _speech(analysis: Mapping[str, Any] | None) -> Mapping[str, Any]:
    understanding = analysis.get("understanding") if isinstance(analysis, Mapping) else None
    speech = understanding.get("speech") if isinstance(understanding, Mapping) else None
    return speech if isinstance(speech, Mapping) else {}


def _segments(analysis: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    rows = _speech(analysis).get("segments")
    return [row for row in rows or [] if isinstance(row, Mapping)]


def _transcripts(analysis: Mapping[str, Any] | None) -> list[str]:
    """Every transcript a clip record carries, each matched on its own."""
    if not isinstance(analysis, Mapping):
        return []
    texts = [analysis.get("transcript"), _speech(analysis).get("transcript")]
    texts.append(" ".join(str(row.get("text") or "") for row in _segments(analysis)))
    return [text for text in texts if isinstance(text, str) and text.strip()]


def transcript_says(analysis: Mapping[str, Any] | None, quote: str) -> bool:
    target = fold_tokens(quote)
    return any(_says(fold_tokens(text), target) for text in _transcripts(analysis))


def is_outro_hallucination(text: str, analysis: Mapping[str, Any] | None = None) -> bool:
    """whisper's "thanks for watching" that the clip's own (Gemini) transcript never heard."""
    tokens = fold_tokens(text)
    if not any(_find_all(tokens, list(phrase)) for phrase in _OUTRO_HALLUCINATIONS):
        return False
    untimed = analysis.get("transcript") if isinstance(analysis, Mapping) else None
    return not (isinstance(untimed, str) and _says(fold_tokens(untimed), tokens))


def regound_quoted_intents(
    intents: Sequence[ResolvedClipIntent], clips: Sequence[Any]
) -> tuple[list[ResolvedClipIntent], int]:
    """Seat a quoted spoken line on the clip whose transcript says it.

    Only a resolved, single-clip intent whose attribute quotes words is checked. When its
    clip's transcript does not say the quote and exactly one other clip's does, the
    intent moves there. A quote no transcript has (on-screen text, a missed transcript)
    or two clips say is left as the resolver had it. Returns ``(intents, moved count)``.
    """
    records = {
        str(getattr(clip, "media_id", "")): getattr(clip, "analysis", None) for clip in clips
    }
    out: list[ResolvedClipIntent] = []
    moved = 0
    for intent in intents:
        quote = spoken_quote(intent.attribute)
        if intent.status != "resolved" or len(intent.assignments) != 1 or not quote:
            out.append(intent)
            continue
        seat = intent.assignments[0]
        sayers = [
            media_id for media_id, record in records.items() if transcript_says(record, quote)
        ]
        if seat.media_id in sayers or len(sayers) != 1:
            out.append(intent)
            continue
        moved += 1
        log.info(
            "clip_intent_quote_reseated",
            intent_id=intent.intent_id,
            from_media_id=seat.media_id,
            to_media_id=sayers[0],
        )
        out.append(
            intent.model_copy(
                update={
                    "assignments": [
                        seat.model_copy(
                            update={
                                "media_id": sayers[0],
                                "evidence": f"transcript says: {quote}"[:240],
                                "confidence": max(seat.confidence, 0.9),
                                "grounding": "record_span",
                            }
                        )
                    ]
                }
            )
        )
    return out, moved


def closing_quote_request(strategy: Mapping[str, Any]) -> tuple[str, str, str] | None:
    """(intent id, media id, quote) of a resolved "end on the moment I say '...'" ask."""
    for row in strategy.get("resolved_clip_intents") or []:
        if not isinstance(row, Mapping):
            continue
        if row.get("op") != "order" or row.get("position") != "last":
            continue
        assignments = [a for a in row.get("assignments") or [] if isinstance(a, Mapping)]
        quote = spoken_quote(str(row.get("attribute") or ""))
        if row.get("status", "resolved") != "resolved" or len(assignments) != 1 or not quote:
            continue
        media_id = assignments[0].get("media_id")
        if isinstance(media_id, str) and media_id:
            return str(row.get("intent_id") or ""), media_id, quote
    return None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def locate_in_segments(
    quote: str, segments: Sequence[Mapping[str, Any]], duration_s: float
) -> tuple[float, float] | None:
    """The shortest run of timed sentences that says ``quote``, padded, else None."""
    target = fold_tokens(quote)
    rows = sorted(
        (
            (start, end, str(row.get("text") or ""))
            for row in segments
            if (start := _number(row.get("start_s"))) is not None
            and (end := _number(row.get("end_s"))) is not None
            and 0 <= start < end
        ),
        key=lambda row: row[0],
    )
    for run in range(1, _MAX_SEGMENT_RUN + 1):
        for first in range(0, len(rows) - run + 1):
            window = rows[first : first + run]
            if _says(fold_tokens(" ".join(text for _s, _e, text in window)), target):
                start = max(0.0, window[0][0] - SEGMENT_LEAD_S)
                end = min(duration_s, window[-1][1] + SEGMENT_TAIL_S)
                return round(start, 3), round(end, 3)
    return None


def locate_in_words(
    quote: str, words: Sequence[Mapping[str, Any]], duration_s: float
) -> tuple[float, float] | None:
    """The quote's word-timed window, widened to its sentence when that is close."""
    grounded = ground_excerpt(list(words), quote, duration_s=duration_s)
    if grounded is None:
        return None
    start, end = grounded.start_s, grounded.end_s
    for sentence in words_to_segments(list(words)):
        if sentence.start_s - 1e-6 <= grounded.word_end_s <= sentence.end_s + 1e-6:
            if sentence.end_s - grounded.word_end_s <= SENTENCE_REACH_S:
                end = max(end, min(duration_s, sentence.end_s + EXCERPT_TAIL_S))
            if grounded.word_start_s - sentence.start_s <= SENTENCE_REACH_S:
                start = min(start, sentence.start_s)
            break
    return round(start, 3), round(end, 3)


def transcribe_stored_quote(
    gcs_path: str, *, language: str | None = None, prompt: str | None = None
) -> tuple[list[dict[str, Any]], str]:
    """Word-timed whisper of one stored proxy, biased toward the quoted words. May raise."""
    from app.pipeline.transcribe import transcribe_whisper_cached  # noqa: PLC0415
    from app.storage import download_to_file  # noqa: PLC0415

    with tempfile.TemporaryDirectory(prefix="spoken-quote-") as tmp:
        local = f"{tmp}/clip.mp4"
        download_to_file(gcs_path, local)
        transcript = transcribe_whisper_cached(local, language=language, verbatim_prompt=prompt)
    words = [
        {"text": w.text, "start_s": float(w.start_s), "end_s": float(w.end_s)}
        for w in transcript.words
    ]
    return words, str(getattr(transcript, "language", "") or "")


async def ground_closing_quote(
    strategy: Mapping[str, Any],
    media_snapshot: Mapping[str, Any] | None,
    *,
    transcribe: Transcribe | None = transcribe_stored_quote,
    timeout_s: float = WHISPER_TIMEOUT_S,
) -> dict[str, Any] | None:
    """Where the quoted closing line is said, as the length plan's pinned closing window.

    ``None`` when the strategy ends on no quoted line. Otherwise a record with
    ``status="found"`` and the padded source window, or ``status="not_found"`` (never a
    guessed window) when neither the timed segments nor a word-timed pass has the line.
    """
    request = closing_quote_request(strategy)
    if request is None or not isinstance(media_snapshot, Mapping):
        return None
    intent_id, media_id, quote = request
    row = next(
        (
            row
            for row in media_snapshot.get("clip_assignments") or []
            if isinstance(row, Mapping) and row.get("media_id") == media_id
        ),
        None,
    )
    record: dict[str, Any] = {
        "intent_id": intent_id,
        "media_id": media_id,
        "quote": quote,
        "status": "not_found",
    }
    duration_s = _number(row.get("duration_s")) if row is not None else None
    if row is None or row.get("kind", "video") != "video" or not duration_s:
        return record
    analysis = row.get("analysis") if isinstance(row.get("analysis"), Mapping) else {}
    span = locate_in_segments(quote, _segments(analysis), duration_s)
    source = "segments"
    gcs_path = row.get("gcs_path") or row.get("proxy_path")
    if span is None and transcribe is not None and isinstance(gcs_path, str) and gcs_path:
        language = str(_speech(analysis).get("language") or "").strip().lower()
        try:
            words, _language = await asyncio.wait_for(
                asyncio.to_thread(
                    transcribe,
                    gcs_path,
                    language=language if re.fullmatch(r"[a-z]{2}", language) else None,
                    prompt=quote,
                ),
                timeout=timeout_s,
            )
        except Exception as exc:  # noqa: BLE001 - a missed line is reported, never fatal
            log.warning(
                "closing_quote_transcribe_failed", media_id=media_id, error=type(exc).__name__
            )
            words = []
        span = locate_in_words(quote, words, duration_s)
        source = "words"
    if span is None:
        log.info("closing_quote_not_found", media_id=media_id)
        return record
    log.info(
        "closing_quote_found", media_id=media_id, source=source, start_s=span[0], end_s=span[1]
    )
    return {**record, "status": "found", "source": source, "start_s": span[0], "end_s": span[1]}


def closing_quote_notice(record: Mapping[str, Any] | None) -> str | None:
    """What the reply must say when the quoted ending can't be honoured exactly."""
    if not isinstance(record, Mapping) or record.get("status") == "found":
        return None
    quote = str(record.get("quote") or "")
    if record.get("status") == "not_held":
        return say(
            en=f"I found where you say “{quote}”, but couldn't fit it as the ending with "
            "your other clips, so the video won't end exactly on it.",
            tr=f"“{quote}” dediğin anı buldum ama diğer kliplerle birlikte sona "
            "yerleştiremedim, bu yüzden video tam o anda bitmeyecek.",
        )
    if record.get("status") == "too_long":
        return say(
            en=f"The line “{quote}” is longer than the video can hold with your other "
            "moments, so the video won't end exactly on it.",
            tr=f"“{quote}” cümlesi diğer anlarla birlikte videoya sığmıyor, bu yüzden video "
            "tam o anda bitmeyecek.",
        )
    return say(
        en=f"I couldn't find the moment you say “{quote}” in your clips, so the video "
        "can't end exactly on it.",
        tr=f"Kliplerinde “{quote}” dediğin anı bulamadım, bu yüzden video tam o anda bitemiyor.",
    )


__all__ = [
    "closing_quote_notice",
    "closing_quote_request",
    "ground_closing_quote",
    "is_outro_hallucination",
    "locate_in_segments",
    "locate_in_words",
    "quoted_phrases",
    "regound_quoted_intents",
    "spoken_quote",
    "transcribe_stored_quote",
    "transcript_says",
]
