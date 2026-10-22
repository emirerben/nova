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

KRI-681 (Tigre night M1, thread 133bdded): "tam 20 dakika sürdü dediğim klip en sonda
olsun ve o cümle duyulsun" names the line without quote marks, and the clip's one timed
segment ran 3.8-13.6 s, so the closing cut played 0-2.2 s and the line was never heard:

* ``said_line`` -- the longest run of an ask's words the seated clip's transcript says,
  so an unquoted line grounds on the clip's own words, never on a guess at grammar.
* a line found inside a long timed segment is narrowed to the line itself (word-timed
  pass, else its place in the segment's text).
* ``described_line_moments`` -- "the clip where the boat won't start: keep its sound"
  seats that clip's window on the sentence that says it ("tekne çalışmıyor").
"""

from __future__ import annotations

import asyncio
import re
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import structlog

from app.kria.reply_language import say
from app.schemas.clip_intents import ResolvedClipIntent

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
# KRI-681: a found line longer than this is a long segment that also holds other words;
# it is narrowed to the line. A line placed by its share of the segment's text keeps this
# much extra either side, since speech is not spoken at an even rate.
LONG_LINE_S = 5.0
ESTIMATE_LEAD_S = 1.0
ESTIMATE_TAIL_S = 0.6
# An unquoted line is at least two content words (a number, or 4+ letters), so "the
# river" never grounds on a clip that happens to say "the river".
_CONTENT_MIN_CHARS = 4
_MIN_CONTENT_WORDS = 2
_MAX_ASK_WORDS = 24
# A described clip matches a sentence on two word stems: "teknenin çalışmadığı" and
# "tekne çalışmıyor" share "tekne" and "calis".
_STEM_CHARS = 5
_MIN_SHARED_STEMS = 2
# A described sentence's window: the sentence with a breath either side, at least this
# long so a short call ("Amigo Sergio") still reads as a shot.
DESCRIBED_MOMENT_MIN_S = 2.5
# Segment times run a little late on a quiet start (the boat clip's "Tekne" at 3.6 s was
# timed 4.04 s), so a described sentence keeps more breath before it than after.
DESCRIBED_LEAD_S = 0.8
_CLAUSE_BREAKS = ",;:.!?\n"
_EDGE_PUNCTUATION = "\"'“”‘’«»„()[]{},;:.!?…"

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


def quoted_texts(text: str | None) -> list[str]:
    """Every quoted run in ``text`` as written: no word-count floor and no word folding, so
    it never imports the grounding stack (the Main Creator output parser uses it)."""
    return [
        next(group for group in match.groups() if group is not None).strip()
        for match in _QUOTED_RE.finditer(str(text or ""))
    ]


def quoted_spans(text: str | None) -> list[tuple[int, int, str]]:
    """``quoted_phrases`` with each quote's (start, end) offsets in ``text``."""
    out: list[tuple[int, int, str]] = []
    raw = str(text or "")
    for match in _QUOTED_RE.finditer(raw):
        phrase = next(group for group in match.groups() if group is not None).strip()
        if len(fold_tokens(phrase)) >= MIN_QUOTE_TOKENS:
            out.append((match.start(), match.end(), phrase))
    return out


def fold_tokens(text: object) -> list[str]:
    # Imported here: phone_reaction_grounding pulls in skia, and this module is on the
    # import path of routes and CLIs that must load without a GPU stack.
    from app.services.phone_reaction_grounding import fold_tokens as fold  # noqa: PLC0415

    return fold(text)


def _find_all(tokens: list[str], target: list[str]) -> bool:
    width = len(target)
    return any(tokens[i : i + width] == target for i in range(len(tokens) - width + 1))


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
    from app.services.speech_segments import _fuzzy_span  # noqa: PLC0415

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


def _content(token: str) -> bool:
    return token.isdigit() or len(token) >= _CONTENT_MIN_CHARS


def said_line(text: str | None, analysis: Mapping[str, Any] | None) -> str | None:
    """The longest run of ``text``'s words the clip's transcript says word for word (KRI-681).

    "tam 20 dakika sürdü dediğim klip" on a clip that says "... tam 20 dakika sürdü ha"
    gives "tam 20 dakika sürdü". None when no run of two content words is said.
    """
    words = [
        word.strip(_EDGE_PUNCTUATION)
        for word in str(text or "").split()[:_MAX_ASK_WORDS]
        if word.strip(_EDGE_PUNCTUATION)
    ]
    folded = [fold_tokens(word) for word in words]
    transcripts = [fold_tokens(text) for text in _transcripts(analysis)]
    if not transcripts:
        return None
    best: tuple[int, str] | None = None
    for first in range(len(words)):
        for last in range(len(words), first, -1):
            tokens = [token for word in folded[first:last] for token in word]
            if best is not None and len(tokens) <= best[0]:
                break
            if sum(_content(token) for token in tokens) < _MIN_CONTENT_WORDS:
                continue
            if any(_find_all(transcript, tokens) for transcript in transcripts):
                best = (len(tokens), " ".join(words[first:last]))
                break
    return best[1] if best is not None else None


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


def _snapshot_row(
    media_snapshot: Mapping[str, Any] | None, media_id: str
) -> Mapping[str, Any] | None:
    if not isinstance(media_snapshot, Mapping):
        return None
    return next(
        (
            row
            for row in media_snapshot.get("clip_assignments") or []
            if isinstance(row, Mapping) and row.get("media_id") == media_id
        ),
        None,
    )


def _row_analysis(row: Mapping[str, Any] | None) -> Mapping[str, Any]:
    analysis = row.get("analysis") if isinstance(row, Mapping) else None
    return analysis if isinstance(analysis, Mapping) else {}


def closing_quote_request(
    strategy: Mapping[str, Any], media_snapshot: Mapping[str, Any] | None = None
) -> tuple[str, str, str] | None:
    """(intent id, media id, line) of a resolved "end on the moment I say ..." ask.

    A quoted line is taken as written. An unquoted one (KRI-681) is the run of the ask's
    words the seated clip says, read off ``media_snapshot``.
    """
    for row in strategy.get("resolved_clip_intents") or []:
        if not isinstance(row, Mapping):
            continue
        if row.get("op") != "order" or row.get("position") != "last":
            continue
        assignments = [a for a in row.get("assignments") or [] if isinstance(a, Mapping)]
        if row.get("status", "resolved") != "resolved" or len(assignments) != 1:
            continue
        media_id = assignments[0].get("media_id")
        if not isinstance(media_id, str) or not media_id:
            continue
        attribute = str(row.get("attribute") or "")
        quote = spoken_quote(attribute) or said_line(
            attribute, _row_analysis(_snapshot_row(media_snapshot, media_id))
        )
        if quote:
            return str(row.get("intent_id") or ""), media_id, quote
    return None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _timed_rows(segments: Sequence[Mapping[str, Any]]) -> list[tuple[float, float, str]]:
    return sorted(
        (
            (start, end, str(row.get("text") or ""))
            for row in segments
            if (start := _number(row.get("start_s"))) is not None
            and (end := _number(row.get("end_s"))) is not None
            and 0 <= start < end
        ),
        key=lambda row: row[0],
    )


def locate_in_segments(
    quote: str, segments: Sequence[Mapping[str, Any]], duration_s: float
) -> tuple[float, float] | None:
    """The shortest run of timed sentences that says ``quote``, padded, else None."""
    target = fold_tokens(quote)
    rows = _timed_rows(segments)
    for run in range(1, _MAX_SEGMENT_RUN + 1):
        for first in range(0, len(rows) - run + 1):
            window = rows[first : first + run]
            if _says(fold_tokens(" ".join(text for _s, _e, text in window)), target):
                start = max(0.0, window[0][0] - SEGMENT_LEAD_S)
                end = min(duration_s, window[-1][1] + SEGMENT_TAIL_S)
                return round(start, 3), round(end, 3)
    return None


def _token_offsets(text: str) -> list[tuple[str, int, int]]:
    """Each folded token of ``text`` with the character span of the word it came from."""
    out: list[tuple[str, int, int]] = []
    cursor = 0
    for word in text.split():
        start = text.find(word, cursor)
        cursor = start + len(word)
        out.extend((token, start, cursor) for token in fold_tokens(word))
    return out


def _token_span(tokens: list[str], target: list[str]) -> tuple[int, int] | None:
    width = len(target)
    for first in range(len(tokens) - width + 1):
        if width and tokens[first : first + width] == target:
            return first, first + width - 1
    from app.services.speech_segments import _fuzzy_span  # noqa: PLC0415

    fuzzy = _fuzzy_span(tokens, target)
    return (fuzzy[0], fuzzy[1]) if fuzzy is not None else None


def estimate_in_segments(
    quote: str, segments: Sequence[Mapping[str, Any]], duration_s: float
) -> tuple[float, float] | None:
    """The quote's window inside the one timed segment that says it (KRI-681).

    For a segment that also holds other sentences when no word timings are at hand: the
    line's place is its share of the segment's text, kept ``ESTIMATE_LEAD_S`` /
    ``ESTIMATE_TAIL_S`` wider, and runs to the segment's end when that is close. Never
    leaves the segment.
    """
    target = fold_tokens(quote)
    for start, end, text in _timed_rows(segments):
        offsets = _token_offsets(text)
        span = _token_span([token for token, _a, _b in offsets], target)
        if span is None or not text:
            continue
        seconds, length = end - start, len(text)
        line_start = start + seconds * offsets[span[0]][1] / length
        line_end = start + seconds * offsets[span[1]][2] / length
        low = max(0.0, start - SEGMENT_LEAD_S, line_start - ESTIMATE_LEAD_S)
        high = (
            end + SEGMENT_TAIL_S
            if end - line_end <= SENTENCE_REACH_S
            else line_end + ESTIMATE_TAIL_S
        )
        high = min(duration_s, end + SEGMENT_TAIL_S, high)
        if high > low:
            return round(low, 3), round(high, 3)
    return None


def locate_in_words(
    quote: str, words: Sequence[Mapping[str, Any]], duration_s: float
) -> tuple[float, float] | None:
    """The quote's word-timed window, widened to its sentence when that is close."""
    from app.services.speech_segments import (  # noqa: PLC0415
        EXCERPT_TAIL_S,
        ground_excerpt,
        words_to_segments,
    )

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


async def _word_span(
    transcribe: Transcribe | None,
    row: Mapping[str, Any],
    quote: str,
    duration_s: float,
    timeout_s: float,
) -> tuple[float, float] | None:
    """The line's window from a word-timed whisper pass of the stored proxy, else None."""
    gcs_path = row.get("gcs_path") or row.get("proxy_path")
    if transcribe is None or not isinstance(gcs_path, str) or not gcs_path:
        return None
    language = str(_speech(_row_analysis(row)).get("language") or "").strip().lower()
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
            "closing_quote_transcribe_failed",
            media_id=row.get("media_id"),
            error=type(exc).__name__,
        )
        words = []
    return locate_in_words(quote, words, duration_s)


async def ground_closing_quote(
    strategy: Mapping[str, Any],
    media_snapshot: Mapping[str, Any] | None,
    *,
    transcribe: Transcribe | None = transcribe_stored_quote,
    timeout_s: float = WHISPER_TIMEOUT_S,
) -> dict[str, Any] | None:
    """Where the closing line is said, as the length plan's pinned closing window.

    ``None`` when the strategy ends on no named line. Otherwise a record with
    ``status="found"`` and the padded source window, or ``status="not_found"`` (never a
    guessed window) when neither the timed segments nor a word-timed pass has the line.
    A line inside a long segment (KRI-681) is narrowed to the line: by word timings when
    a word-timed pass has it, else by its place in the segment's text.
    """
    request = closing_quote_request(strategy, media_snapshot)
    if request is None or not isinstance(media_snapshot, Mapping):
        return None
    intent_id, media_id, quote = request
    row = _snapshot_row(media_snapshot, media_id)
    record: dict[str, Any] = {
        "intent_id": intent_id,
        "media_id": media_id,
        "quote": quote,
        "status": "not_found",
    }
    duration_s = _number(row.get("duration_s")) if row is not None else None
    if row is None or row.get("kind", "video") != "video" or not duration_s:
        return record
    segments = _segments(_row_analysis(row))
    span = locate_in_segments(quote, segments, duration_s)
    source = "segments"
    if span is None:
        span = await _word_span(transcribe, row, quote, duration_s, timeout_s)
        source = "words"
    elif span[1] - span[0] > LONG_LINE_S:
        narrowed = await _word_span(transcribe, row, quote, duration_s, timeout_s)
        source = "words"
        if narrowed is None:
            narrowed = estimate_in_segments(quote, segments, duration_s)
            source = "segment_estimate"
        if narrowed is not None and narrowed[1] - narrowed[0] < span[1] - span[0]:
            span = narrowed
        else:
            source = "segments"
    if span is None:
        log.info("closing_quote_not_found", media_id=media_id)
        return record
    log.info(
        "closing_quote_found", media_id=media_id, source=source, start_s=span[0], end_s=span[1]
    )
    return {**record, "status": "found", "source": source, "start_s": span[0], "end_s": span[1]}


def _stems(text: object) -> set[str]:
    return {
        token if token.isdigit() else token[:_STEM_CHARS]
        for token in fold_tokens(text)
        if token.isdigit() or len(token) >= _STEM_CHARS
    }


def _clauses(text: str) -> list[str]:
    for mark in _CLAUSE_BREAKS:
        text = text.replace(mark, "\n")
    return [clause for clause in text.split("\n") if clause.strip()]


def _match_sentence(
    clause: str, sentences: Sequence[tuple[str, float, float, float, set[str]]]
) -> tuple[str, tuple[int, str, float, float, float] | None]:
    """("found", best) / ("ambiguous", None) / ("none", None) for one clause of an ask.

    ``best`` is (shared stems, media id, clip duration, start, end) of the one sentence on
    two or more shared word stems; two clips saying it equally is ambiguous.
    """
    want = _stems(clause)
    hits = [
        (len(want & stems), media_id, duration, start, end)
        for media_id, duration, start, end, stems in sentences
        if len(want & stems) >= _MIN_SHARED_STEMS
    ]
    if not hits:
        return "none", None
    top = max(hit[0] for hit in hits)
    best = [hit for hit in hits if hit[0] == top]
    if len({hit[1] for hit in best}) != 1:
        log.info("described_line_ambiguous", media_ids=sorted({h[1] for h in best}))
        return "ambiguous", None
    return "found", min(best, key=lambda hit: hit[3])


def _sentences(
    media_snapshot: Mapping[str, Any] | None, skip_ids: Sequence[str]
) -> list[tuple[str, float, float, float, set[str]]]:
    rows = [
        row
        for row in (media_snapshot or {}).get("clip_assignments") or []
        if isinstance(row, Mapping)
        and isinstance(row.get("media_id"), str)
        and row["media_id"] not in skip_ids
        and row.get("kind", "video") == "video"
        and _number(row.get("duration_s"))
    ]
    return [
        (row["media_id"], float(row["duration_s"]), start, end, _stems(text))
        for row in rows
        for start, end, text in _timed_rows(_segments(_row_analysis(row)))
    ]


def _padded_window(duration: float, start: float, end: float) -> tuple[float, float]:
    low = max(0.0, start - DESCRIBED_LEAD_S)
    high = min(duration, max(end + SEGMENT_TAIL_S, low + DESCRIBED_MOMENT_MIN_S))
    low = max(0.0, min(low, high - DESCRIBED_MOMENT_MIN_S))
    return round(low, 3), round(high, 3)


@dataclass(frozen=True)
class DescribedLine:
    """A clip's window and the timed sentence (source seconds) that seated it."""

    window: tuple[float, float]
    line: tuple[float, float]


def described_lines(
    texts: Sequence[str],
    media_snapshot: Mapping[str, Any] | None,
    *,
    skip_ids: Sequence[str] = (),
    lines: Sequence[str] = (),
) -> dict[str, DescribedLine]:
    """The spoken sentence each ask describes a clip by, as that clip's source window.

    "Teknenin çalışmadığı klipte kendi sesi açık kalsın" names no line, but the boat clip
    says "tekne çalışmıyor" (KRI-681). Each clause of ``texts`` (the creator's audio and
    order asks) is matched on word stems against
    every timed sentence; the clip with the one best match (two shared stems or more) gets
    that sentence, padded to at least ``DESCRIBED_MOMENT_MIN_S``. A tie between clips moves
    nothing; the first ask to seat a clip wins; ``skip_ids`` (a held closing line) are never
    touched. ``lines`` (KRI-760) are lines the creator wants heard whole, each matched as
    one clause whatever commas it has, and seated before the clauses of ``texts``.
    """
    sentences = _sentences(media_snapshot, skip_ids)
    out: dict[str, DescribedLine] = {}
    for clauses in ([line] for line in lines), (_clauses(str(text or "")) for text in texts):
        for clause in (clause for group in clauses for clause in group):
            status, hit = _match_sentence(clause, sentences)
            if status != "found" or hit is None:
                continue
            _shared, media_id, duration, start, end = hit
            if media_id in out:
                continue
            window = _padded_window(duration, start, end)
            out[media_id] = DescribedLine(window=window, line=(start, end))
            log.info("described_line_seated", media_id=media_id, start_s=window[0], end_s=window[1])
    return out


def described_line_moments(
    texts: Sequence[str],
    media_snapshot: Mapping[str, Any] | None,
    *,
    skip_ids: Sequence[str] = (),
) -> dict[str, tuple[float, float]]:
    """``described_lines``' windows only (KRI-681)."""
    found = described_lines(texts, media_snapshot, skip_ids=skip_ids)
    return {media_id: described.window for media_id, described in found.items()}


def named_line_records(
    quotes: Sequence[str],
    media_snapshot: Mapping[str, Any] | None,
    *,
    skip_ids: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """Where each line the creator wants heard whole is said (KRI-760), for the receipt.

    One record per quote: ``status`` "found" with the clip and the sentence's source
    seconds, "ambiguous" when two clips say it equally, "not_found" when no clip does.
    A line said on a ``skip_ids`` clip (a held closing line) is left out: that clip's
    window already belongs to the ending.
    """
    everywhere = _sentences(media_snapshot, ())
    out: list[dict[str, Any]] = []
    for quote in quotes:
        status, hit = _match_sentence(quote, everywhere)
        if status == "found" and hit is not None:
            _shared, media_id, _duration, start, end = hit
            if media_id in skip_ids:
                continue
            out.append(
                {
                    "quote": quote,
                    "status": "found",
                    "media_id": media_id,
                    "start_s": round(start, 3),
                    "end_s": round(end, 3),
                }
            )
        else:
            missing = "ambiguous" if status == "ambiguous" else "not_found"
            out.append({"quote": quote, "status": missing})
    return out


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
    "DescribedLine",
    "closing_quote_notice",
    "closing_quote_request",
    "described_line_moments",
    "described_lines",
    "estimate_in_segments",
    "ground_closing_quote",
    "is_outro_hallucination",
    "locate_in_segments",
    "locate_in_words",
    "named_line_records",
    "quoted_phrases",
    "quoted_spans",
    "quoted_texts",
    "regound_quoted_intents",
    "said_line",
    "spoken_quote",
    "transcribe_stored_quote",
    "transcript_says",
]
