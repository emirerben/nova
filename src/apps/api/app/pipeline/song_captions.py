"""Song lyrics as a montage caption lane (KRI-569).

The creator's uploaded song is transcribed once at attach time
(``SongAnalysis.words``). When they ask for captions on a song montage, the planner pins
those words on the snapshot as a ``SongCaptionTrack`` (song seconds). The guided
compiler then projects the words that fall inside the song window onto the output
clock, as ordinary ``caption_cue`` text rows: the same rows, editor panel and chat ops
as narration captions.

Pure timing math only. Building the ``TextElement`` rows and the editor revision rules
live in ``guided_story`` next to the narration caption lane they mirror.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from app.schemas.user_song import (
    MAX_SONG_CAPTION_WORDS,
    SongAnalysis,
    SongCaptionTrack,
    SongCaptionWord,
)

SONG_CAPTION_ID_PREFIX = "song-caption-"
SONG_CAPTION_ORIGIN = "song"
# A caption that starts this close to the end of the window is not readable in time.
_TAIL_CUTOFF_S = 0.2
# Whisper emits point timestamps for fast sung words; a caption needs a readable beat.
_MIN_WORD_S = 0.12
# One caption layer per drawn word. `EditRecipeV2.text_layers` holds 500 in all (the title,
# closing text, a label per clip, pins and any text the composer adds share them), so the
# lane gets what is left after a reserve. Lockstep with the recipe cap is pinned by
# `tests/pipeline/test_song_captions.py::test_the_layer_budget_matches_the_recipe_cap`.
MAX_PROJECTED_WORDS = 400
RECIPE_TEXT_LAYERS = 500
# Title + closing text + headroom for text the composer splits into words.
_RESERVED_LAYERS = 62
# Styles the creator can name for captions; "none" is handled by the callers.
_WORD_STYLES = frozenset({"karaoke", "kinetic"})
_LIPSYNC_RE = re.compile(r"\blip[\s-]?sync\w*|\blipsync\w*|\bdudak\s+senkron\w*", re.IGNORECASE)


@dataclass(frozen=True)
class ProjectedWord:
    index: int
    text: str
    start_s: float
    end_s: float
    line_end: bool


def song_caption_id(index: int) -> str:
    return f"{SONG_CAPTION_ID_PREFIX}{index}"


def song_caption_index(element_id: object) -> int | None:
    """The track index a ``song-caption-N`` id names, else None."""
    if not isinstance(element_id, str) or not element_id.startswith(SONG_CAPTION_ID_PREFIX):
        return None
    tail = element_id[len(SONG_CAPTION_ID_PREFIX) :]
    return int(tail) if tail.isdigit() else None


def caption_style_for(strategy_style: str | None, *, word_ask: bool = False) -> str:
    """ "word" for karaoke/kinetic asks (one word at a time), else whole lyric lines."""
    return "word" if word_ask or strategy_style in _WORD_STYLES else "sentence"


def mentions_lipsync(text: str) -> bool:
    """The creator's own words say this is a lip-sync ("lipsync", "lip sync", "lip-sync")."""
    return bool(_LIPSYNC_RE.search(text or ""))


def track_from_analysis(analysis: SongAnalysis, *, caption_style: str) -> SongCaptionTrack | None:
    """The whole song's words with their lyric-line ends, or None when nothing was heard."""
    # Whisper can emit a word out of order; the track (and the clock) need them sorted.
    words = sorted(
        (word for word in analysis.words if word.text.strip()),
        key=lambda word: (float(word.start_s), float(word.end_s)),
    )[:MAX_SONG_CAPTION_WORDS]
    if not words:
        return None
    line_ends = _line_end_indexes(words, analysis)
    track_words = [
        SongCaptionWord(
            text=word.text.strip()[:80],
            start_s=round(float(word.start_s), 3),
            end_s=round(max(float(word.end_s), float(word.start_s)), 3),
            line_end=index in line_ends,
        )
        for index, word in enumerate(words)
    ]
    return SongCaptionTrack(
        song_generation=analysis.generation,
        caption_style="word" if caption_style == "word" else "sentence",
        words=track_words,
    )


def _line_end_indexes(words: list, analysis: SongAnalysis) -> set[int]:
    """Indexes of the last word of each lyric line (``SongAnalysis.lines``).

    A line owns the words that start inside its span; the last of them closes it.
    Words outside every line (a stray tail) still close at the final word.
    """
    ends: set[int] = set()
    cursor = 0
    for line in sorted(analysis.lines, key=lambda row: row.start_s):
        last: int | None = None
        while cursor < len(words) and float(words[cursor].start_s) < float(line.end_s) + 1e-3:
            if float(words[cursor].start_s) + 1e-3 >= float(line.start_s):
                last = cursor
            cursor += 1
        if last is not None:
            ends.add(last)
    if words:
        ends.add(len(words) - 1)
    return ends


def lyric_word_budget(*, label_count: int = 0, pin_count: int = 0) -> int:
    """How many lyric words the phone recipe can draw next to the montage's other text."""
    left = RECIPE_TEXT_LAYERS - _RESERVED_LAYERS - max(0, label_count) - max(0, pin_count)
    return max(0, min(MAX_PROJECTED_WORDS, left))


def window_words(
    track: SongCaptionTrack, window_start_s: float, window_end_s: float
) -> list[ProjectedWord]:
    """Every word readable inside the song window, on the output clock (no cap).

    Output time is ``song_time - window_start``. A word must start inside the window and
    at least ``_TAIL_CUTOFF_S`` before it ends; its end is clamped to the window.
    """
    length = round(float(window_end_s) - float(window_start_s), 3)
    if length <= 0:
        return []
    out: list[ProjectedWord] = []
    for index, word in enumerate(track.words):
        if float(word.start_s) + 1e-3 < float(window_start_s):
            continue
        start = round(float(word.start_s) - float(window_start_s), 3)
        if start >= length - _TAIL_CUTOFF_S:
            continue
        end = round(min(float(word.end_s) - float(window_start_s), length), 3)
        if end - start < _MIN_WORD_S:
            end = round(min(start + _MIN_WORD_S, length), 3)
        if end <= start:
            continue
        out.append(
            ProjectedWord(
                index=index, text=word.text, start_s=start, end_s=end, line_end=word.line_end
            )
        )
    return out


def cap_on_line_end(words: list[ProjectedWord], limit: int) -> list[ProjectedWord]:
    """At most ``limit`` words, cut after the last complete lyric line when there is one."""
    if len(words) <= limit:
        return words
    for position in range(limit - 1, -1, -1):
        if words[position].line_end:
            return words[: position + 1]
    return words[:limit]


def project_words(
    track: SongCaptionTrack,
    window_start_s: float,
    window_end_s: float,
    *,
    limit: int | None = None,
) -> list[ProjectedWord]:
    """The words drawn for a window: all of them, capped (on a line end) at ``limit``."""
    cap = MAX_PROJECTED_WORDS if limit is None else limit
    return cap_on_line_end(window_words(track, window_start_s, window_end_s), cap)


def project_snapshot_words(
    snapshot: Any, window: tuple[float, float] | None = None
) -> list[ProjectedWord]:
    """The lyric words a snapshot draws, within the recipe's remaining text-layer budget."""
    track = snapshot.song_captions
    song = snapshot.user_song
    if track is None or (window is None and song is None):
        return []
    span = window if window is not None else (song.window_start_s, song.window_end_s)
    limit = lyric_word_budget(
        label_count=len(snapshot.clip_labels or []), pin_count=len(snapshot.pinned_texts or [])
    )
    return project_words(track, span[0], span[1], limit=limit)


# ── the creator's captions ask (KRI-569) ──────────────────────────────────────
#
# Read on text folded by ``loose_text`` (lower case, Turkish letters and apostrophes
# flattened: "don't" -> "dont", "altyazısız" -> "altyazisiz").
_CAPTION_WORD = r"(?:captions?|subtitles?|karaoke|altyazi\w*)"
_NEGATOR = r"(?:no|not|dont|never|without|cant|skip|drop|remove|hide|disable|stop|do not|turn off)"
# "don't add captions", "I don't want captions", "without any captions", "no lyrics needed".
_NEGATED_BEFORE = re.compile(rf"\b{_NEGATOR}\b(?:\s+\w+){{0,4}}?\s+(?:{_CAPTION_WORD}|lyrics?)\b")
# "captions off", "lyrics not needed".
_NEGATED_AFTER = re.compile(rf"\b(?:{_CAPTION_WORD}|lyrics?)\s+(?:off|not needed|unnecessary)\b")
_NEGATED_TR = re.compile(
    r"altyazi\w*\s+(?:istemiyorum|istemem|olmasin|yok|gerekmiyor|kapali)\b"
    r"|altyazisiz|\bsarki sozleri\w*\s+(?:istemiyorum|olmasin|yok)\b"
)
# The word captions/subtitles/karaoke is an ask by itself; "lyrics" only with an intent
# ("add the lyrics", "lyrics on screen"), never as the topic of the song.
_ASKS_LYRICS = re.compile(
    r"\b(?:add|show|put|display|overlay|include)\s+(?:the\s+|my\s+|its\s+|some\s+|song\s+|sung\s+)*"
    r"lyrics?\b"
    r"|\blyrics?\s+(?:on|over)\s+(?:the\s+|my\s+)?(?:screen|video|clips?|footage)\b"
    r"|\bsarki sozleri\w*\s+(?:ekle|goster|koy|yaz)\w*"
    r"|\b(?:ekle|goster|koy|yaz)\w*\s+sarki sozleri"
)
_ASKS_CAPTIONS = re.compile(rf"\b{_CAPTION_WORD}")


def _folded(line: str) -> str:
    from app.kria.brief_route import loose_text  # noqa: PLC0415

    return loose_text(line).replace("\u2019", "").replace("'", "")


def text_asks_for_captions(text: str) -> bool:
    """The creator's words ask for captions / subtitles / lyrics on screen.

    The request is read in order and the last line that speaks of captions decides, so a
    later "add captions" overrides an earlier "no captions". A bare mention of lyrics
    ("I mouth the lyrics to my song", "use a song with lyrics") is not an ask, and every
    negation form ("don't add captions", "without any captions", "altyazı istemiyorum")
    is a no.
    """
    asks = False
    for line in (text or "").splitlines():
        folded = _folded(line)
        if (
            _NEGATED_BEFORE.search(folded)
            or _NEGATED_AFTER.search(folded)
            or _NEGATED_TR.search(folded)
        ):
            asks = False
        elif _ASKS_CAPTIONS.search(folded) or _ASKS_LYRICS.search(folded):
            asks = True
    return asks


def captions_requested(texts: Iterable[str], caption_style: str | None) -> bool:
    """The creator asked for captions in their own words (and did not switch them off).

    ``caption_style`` is the model's creative default and never a request by itself:
    only the creator's words (and the brief lines written from them) turn lyric captions
    on. ``none`` is an explicit opt-out.
    """
    if caption_style == "none":
        return False
    return text_asks_for_captions("\n".join(text for text in texts if text))
