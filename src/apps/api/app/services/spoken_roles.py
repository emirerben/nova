"""What a clip's own words do in a montage that keeps the camera sound (KRI-585).

A phone montage with no song plays every clip's own sound, so the creator's narration
is heard cut by cut. Prod thread 7CCBDE0A (cabin kit M1): the clip that says "Tigre'de
ev turumuza hoş geldiniz" (welcome to our home tour) played ninth, and the closing clip
("Bu da Deniz, kanalımızın kurucu ekibinden") introduced someone with nothing before
it. Each clip's spoken role is read from its analysed speech:

* ``opening``: the creator welcomes the viewers ("hoş geldiniz", "welcome to", "hi
  guys"); the clip belongs at the start;
* ``closing``: a sign-off ("görüşmek üzere", "see you next time"); it belongs at the end;
* ``introduction``: someone on screen is introduced by name ("Bu da Deniz", "this is
  Deniz", "meet Deniz"); a line that needs something before it.

These are the clips' own words, not the creator's chat message, and no model field
carries a spoken role, so the words are read here. KRI-667: no regex. Words are compared
as ``spoken_quotes.fold_tokens`` tokens (Turkish letters and case folded), against short
phrase lists in the two languages Kria replies in. Narrow on purpose: a bare "merhaba" /
"hello" is nobody's welcome (the introduced person says it), whisper's invented "thanks
for watching" on trailing silence is ignored (``is_outro_hallucination``), and a missed
role leaves the montage exactly as before.

``spoken_seats`` is the one rule the length plan (``kria/planner``), the montage planner
(``pipeline/unified_montage``) and the reply (``kria/narration_notes``) share, so the
order, the windows and what the reply says about them never disagree. Pure functions
over the stored analysis dicts: no I/O, no settings.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cache
from typing import Any, Literal, TypeVar

from app.services.clip_order_sequence import SequenceRow, apply_sequence
from app.services.spoken_quotes import (
    SEGMENT_LEAD_S,
    SEGMENT_TAIL_S,
    fold_tokens,
    is_outro_hallucination,
)

SpokenRole = Literal["opening", "closing", "introduction"]

# A quiet closing cut is at least a readable shot (``unified_montage.DEFAULT_CUT_S``).
MIN_QUIET_S = 1.2
# A held line counts as held when the window covers it to within a frame or two.
HELD_TOLERANCE_S = 0.05

_OPENING_PHRASES = (
    "hoş geldiniz",
    "hoşgeldiniz",
    "hoş geldin",
    "hoşgeldin",
    "herkese merhaba",
    "herkese selam",
    "merhaba arkadaşlar",
    "selam arkadaşlar",
    "merhaba sevgili",
    "selam sevgili",
    "welcome to",
    "welcome back",
    "welcome everyone",
    "welcome everybody",
    "hi guys",
    "hey guys",
    "hello guys",
    "hi everyone",
    "hey everyone",
    "hello everyone",
    "hi everybody",
    "hey everybody",
    "hello everybody",
)
_CLOSING_PHRASES = (
    "izlediğiniz için teşekkür",
    "görüşmek üzere",
    "hoşçakalın",
    "hoşça kalın",
    "bir sonraki videoda",
    "abone olmayı unutmayın",
    "abone olun",
    "kendinize iyi bakın",
    "bugünlük bu kadar",
    "şimdilik bu kadar",
    "bu videoluk bu kadar",
    "thanks for watching",
    "thank you for watching",
    "see you next time",
    "see you in the next",
    "see you soon",
    "until next time",
    "don't forget to subscribe",
    "that's it for today",
    "that's all for today",
)
# A phrase's last word may carry a Turkish suffix ("teşekkür" in "teşekkürler"); a short
# last word ("to", "da") must match whole.
_PREFIX_MIN_CHARS = 4
# Words that may lead a sentence before the introduction itself ("Ve bu da Deniz").
_FILLERS = frozenset({"ve", "evet", "simdi", "and", "so", "now", "okay", "ok"})
# (folded words that introduce a name). The next word must be a capitalised name.
_INTRO_LEADS = (
    ("bu", "da"),
    ("bu", "de"),
    ("bu",),
    ("iste",),
    ("this", "is"),
    ("meet",),
    ("say", "hi", "to"),
    ("say", "hello", "to"),
)
# "Bu da ben" / "this is me": the filmer, not someone introduced.
_NOT_A_NAME = frozenset({"ben", "biz", "me", "us", "i", "we"})
_EDGE_PUNCTUATION = "\"'“”‘’«»„()[]{},;:.!?…"
_APOSTROPHES = ("'", "’")
_SENTENCE_ENDS = (".", "!", "?", "…")

T = TypeVar("T")


@dataclass(frozen=True)
class SpokenLine:
    """The spoken unit that gives a clip its role, timed when the segment is."""

    role: SpokenRole
    text: str
    start_s: float | None = None
    end_s: float | None = None


@dataclass(frozen=True)
class SpokenClip:
    """What ``spoken_seats`` reads of one footage clip (attachment order)."""

    clip_id: str
    analysis: Mapping[str, Any]
    duration_s: float


@dataclass(frozen=True)
class SpokenSeats:
    """Where the clips' own words put them, and the windows that keep the words right.

    ``order`` is the clip ids as the montage plays them (welcomes lead, sign-offs close,
    the creator's own sequence rows seated on top). ``opening`` is the welcome the order
    moved forward, with the source span of its line (held whole); ``quiet_close`` the
    creator-pinned closing clip whose words only introduce someone, with the stretch of
    it that has no words. Empty when no clip has a role.
    """

    order: tuple[str, ...] = ()
    lines: Mapping[str, SpokenLine] = field(default_factory=dict)
    opening: tuple[str, tuple[float, float]] | None = None
    quiet_close: tuple[str, tuple[float, float]] | None = None

    @property
    def roles(self) -> dict[str, SpokenRole]:
        return {clip_id: line.role for clip_id, line in self.lines.items()}

    def record(self) -> dict[str, Any]:
        """JSON-safe: the roles and the two seated windows (ids and seconds only)."""
        out: dict[str, Any] = {"roles": self.roles}
        if self.opening is not None:
            out["opening"] = {
                "media_id": self.opening[0],
                "start_s": self.opening[1][0],
                "end_s": self.opening[1][1],
            }
        if self.quiet_close is not None:
            out["quiet_close"] = {
                "media_id": self.quiet_close[0],
                "start_s": self.quiet_close[1][0],
                "end_s": self.quiet_close[1][1],
            }
        return out


@cache
def _folded_phrases(phrases: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    # Folded on first use: ``fold_tokens`` loads its module lazily (no skia at import).
    return tuple(tuple(fold_tokens(phrase)) for phrase in phrases)


def _says_phrase(tokens: Sequence[str], phrase: Sequence[str]) -> bool:
    width = len(phrase)
    if not width or len(tokens) < width:
        return False
    head, last = list(phrase[:-1]), phrase[-1]
    prefix = len(last) >= _PREFIX_MIN_CHARS
    for i in range(len(tokens) - width + 1):
        word = tokens[i + width - 1]
        if list(tokens[i : i + width - 1]) == head and (
            word == last or (prefix and word.startswith(last))
        ):
            return True
    return False


def _says_any(tokens: Sequence[str], phrases: tuple[str, ...]) -> bool:
    return any(_says_phrase(tokens, phrase) for phrase in _folded_phrases(phrases))


def speech_of(analysis: object) -> Mapping[str, Any] | None:
    """The clip's analysed speech, only when the analysis says it has any."""
    if not isinstance(analysis, Mapping):
        return None
    understanding = analysis.get("understanding")
    speech = understanding.get("speech") if isinstance(understanding, Mapping) else None
    if not isinstance(speech, Mapping) or speech.get("has_speech") is not True:
        return None
    return speech


def _people_on_screen(analysis: Mapping[str, Any]) -> bool:
    understanding = analysis.get("understanding")
    people = understanding.get("people") if isinstance(understanding, Mapping) else None
    count = people.get("count") if isinstance(people, Mapping) else None
    return isinstance(count, int) and not isinstance(count, bool) and count >= 1


def _timed_segments(
    speech: Mapping[str, Any], analysis: Mapping[str, Any]
) -> list[tuple[str, float, float]]:
    rows: list[tuple[str, float, float]] = []
    for segment in speech.get("segments") or []:
        if not isinstance(segment, Mapping):
            continue
        try:
            start, end = float(segment.get("start_s")), float(segment.get("end_s"))
        except (TypeError, ValueError):
            continue
        text = segment.get("text")
        text = text if isinstance(text, str) else ""
        if is_outro_hallucination(text, analysis):
            continue  # whisper's "thanks for watching" on silence: nobody said it
        if math.isfinite(start) and math.isfinite(end) and 0 <= start < end:
            rows.append((text, start, end))
    return sorted(rows, key=lambda row: row[1])


def _sentences(text: str) -> list[str]:
    out: list[str] = []
    current: list[str] = []
    for word in text.split():
        current.append(word)
        if word.rstrip("\"'”’»)").endswith(_SENTENCE_ENDS):
            out.append(" ".join(current))
            current = []
    if current:
        out.append(" ".join(current))
    return out


def _units(
    speech: Mapping[str, Any], analysis: Mapping[str, Any]
) -> list[tuple[str, float | None, float | None]]:
    """Timed segments when the clip has them, else the transcript's sentences."""
    timed: list[tuple[str, float | None, float | None]] = [
        (text, start, end) for text, start, end in _timed_segments(speech, analysis) if text.strip()
    ]
    if timed:
        return timed
    transcript = speech.get("transcript")
    if not isinstance(transcript, str):
        return []
    return [
        (sentence, None, None)
        for sentence in _sentences(transcript)
        if not is_outro_hallucination(sentence, analysis)
    ]


def _introduces(text: str) -> bool:
    words = [word.strip(_EDGE_PUNCTUATION) for word in text.split()]
    words = [word for word in words if word]
    folded = [" ".join(fold_tokens(word)) for word in words]
    start = 0
    while start < len(folded) and folded[start] in _FILLERS:
        start += 1
    for lead in _INTRO_LEADS:
        end = start + len(lead)
        if tuple(folded[start:end]) != lead or end >= len(words):
            continue
        name = words[end]
        possessive = any(mark in name for mark in _APOSTROPHES)  # "Deniz'in odası"
        if name[:1].isupper() and not possessive and folded[end] not in _NOT_A_NAME:
            return True
    return False


def spoken_line(analysis: object) -> SpokenLine | None:
    """The clip's spoken role, read off its first unit (segment or sentence) that has one."""
    speech = speech_of(analysis)
    if speech is None:
        return None
    assert isinstance(analysis, Mapping)  # ``speech_of`` checked it
    person = _people_on_screen(analysis)
    for text, start, end in _units(speech, analysis):
        tokens = fold_tokens(text)
        role: SpokenRole | None = None
        if _says_any(tokens, _OPENING_PHRASES):
            role = "opening"
        elif _says_any(tokens, _CLOSING_PHRASES):
            role = "closing"
        elif person and _introduces(text):
            role = "introduction"
        if role is not None:
            return SpokenLine(role=role, text=text.strip(), start_s=start, end_s=end)
    return None


def line_span(line: SpokenLine, duration_s: float) -> tuple[float, float] | None:
    """The line's own words with a breath either side, kept inside the clip."""
    if line.start_s is None or line.end_s is None:
        return None
    start = max(0.0, line.start_s - SEGMENT_LEAD_S)
    end = min(float(duration_s), line.end_s + SEGMENT_TAIL_S)
    return (round(start, 3), round(end, 3)) if end > start else None


def quiet_window(
    analysis: object, duration_s: float, *, min_s: float
) -> tuple[float, float] | None:
    """The longest stretch of the clip with none of its timed words, if ``min_s`` long.

    Keeps a breath from the words on each side; a clip with no timed segments has no
    known quiet stretch.
    """
    speech = speech_of(analysis)
    if speech is None:
        return None
    assert isinstance(analysis, Mapping)
    segments = _timed_segments(speech, analysis)
    if not segments:
        return None
    duration = float(duration_s)
    # The clip's own start needs no breath after it; every spoken end does.
    gaps: list[tuple[float, float]] = [(0.0, segments[0][1] - SEGMENT_LEAD_S)]
    cursor = segments[0][2]
    for _text, start, end in segments[1:]:
        gaps.append((cursor + SEGMENT_TAIL_S, start - SEGMENT_LEAD_S))
        cursor = max(cursor, end)
    gaps.append((cursor + SEGMENT_TAIL_S, duration))
    best: tuple[float, float] | None = None
    for start, end in gaps:
        start, end = max(0.0, start), min(duration, end)
        if end - start >= min_s - 1e-6 and (best is None or end - start > best[1] - best[0]):
            best = (round(start, 3), round(end, 3))
    return best


def speech_ordered(
    items: Sequence[T], roles: Mapping[str, SpokenRole], key: Callable[[T], str]
) -> list[T]:
    """Welcomes lead, sign-offs close; every other item keeps its place between them."""
    opening = [item for item in items if roles.get(key(item)) == "opening"]
    closing = [item for item in items if roles.get(key(item)) == "closing"]
    moved = {key(item) for item in (*opening, *closing)}
    return [*opening, *(item for item in items if key(item) not in moved), *closing]


def spoken_seats(
    clips: Sequence[SpokenClip],
    rows: Sequence[SequenceRow],
    *,
    hold_closing_words: bool = False,
) -> SpokenSeats:
    """KRI-585: the order and windows a camera-sound montage takes from what the clips say.

    ``clips`` are the edit's footage clips in the order nobody chose (attachment order);
    ``rows`` the creator's resolved sequence (``clip_order_sequence.sequence_rows``), which
    seats on top, so "start with the river" still opens and the welcome follows it.
    ``hold_closing_words`` is True when the creator asked to hear the closing clip
    (original audio, a quoted closing line): its introduction is then never cut around.
    """
    lines = {
        clip.clip_id: line for clip in clips if (line := spoken_line(clip.analysis)) is not None
    }
    if not lines:
        return SpokenSeats(order=tuple(clip.clip_id for clip in clips))
    roles = {clip_id: line.role for clip_id, line in lines.items()}
    ordered = apply_sequence(
        speech_ordered(clips, roles, lambda clip: clip.clip_id), rows, key=lambda c: c.clip_id
    )
    seated = {
        m for _position, _name, members, status in rows if status == "resolved" for m in members
    }
    opening: tuple[str, tuple[float, float]] | None = None
    for clip in ordered[:-1]:
        line = lines.get(clip.clip_id)
        if line is not None and line.role == "opening" and clip.clip_id not in seated:
            span = line_span(line, clip.duration_s)
            opening = (clip.clip_id, span) if span is not None else None
            break
    quiet: tuple[str, tuple[float, float]] | None = None
    closing = ordered[-1]
    line = lines.get(closing.clip_id)
    pinned_last = any(
        position == "last" and status == "resolved" and closing.clip_id in members
        for position, _name, members, status in rows
    )
    if line is not None and line.role == "introduction" and pinned_last and not hold_closing_words:
        window = quiet_window(closing.analysis, closing.duration_s, min_s=MIN_QUIET_S)
        quiet = (closing.clip_id, window) if window is not None else None
    return SpokenSeats(
        order=tuple(clip.clip_id for clip in ordered),
        lines=lines,
        opening=opening,
        quiet_close=quiet,
    )


def window_holds(window: tuple[float, float], span: tuple[float, float]) -> bool:
    """Does the source ``window`` play all of ``span`` (to within a frame or two)?"""
    return window[0] <= span[0] + HELD_TOLERANCE_S and window[1] >= span[1] - HELD_TOLERANCE_S


def window_inside(window: tuple[float, float], span: tuple[float, float]) -> bool:
    """Does the source ``window`` stay inside ``span`` (to within a frame or two)?"""
    return window[0] >= span[0] - HELD_TOLERANCE_S and window[1] <= span[1] + HELD_TOLERANCE_S
