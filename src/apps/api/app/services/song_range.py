"""The stretch of the song a creator filmed in (KRI-580).

Most songs repeat their verses. A take's words match the song at EVERY repeat, so takes filmed in
one stretch of the song get placed at different repeats, far apart, and the planner splits the
group (it keeps one side and drops the rest). The creator knows where they were in the song; when
they say so, a take is only matched inside that stretch.

Applied where alignment is READ (the planner gate and the render worker), never when it is written:
the stored candidates stay complete, so changing or clearing the range takes effect immediately and
needs no re-alignment.

Pure: no I/O.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from app.kria.reply_language import say
from app.schemas.user_song import (
    SONG_RANGE_ANSWER_KEY,
    SONG_RANGE_QUESTION_KEY,
    AlignmentAlternate,
    PlacementCandidate,
    SongAlignment,
    SongRangeAnswerIn,
    SongRangeQuestion,
    TakeAlignment,
)

# A take's song position is where the TAKE STARTS, and creators film with a run-up or a late start,
# so a candidate a little outside the stated stretch still counts. Wider than any run-up, narrower
# than the distance between two repeats of a verse.
SONG_RANGE_TOLERANCE_S = 3.0


@dataclass(frozen=True)
class SongRange:
    start_s: float
    end_s: float

    def contains_start(self, delta_s: float, tolerance_s: float = SONG_RANGE_TOLERANCE_S) -> bool:
        return self.start_s - tolerance_s <= delta_s <= self.end_s + tolerance_s


def parse_song_range(start_s: Any, end_s: Any) -> SongRange | None:
    """A valid range, or None when unset or malformed (never raises: it gates nothing)."""
    for value in (start_s, end_s):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            return None
    start, end = float(start_s), float(end_s)
    if start < 0 or end <= start:
        return None
    return SongRange(start, end)


def song_range_of(item: Any) -> SongRange | None:
    """The range stored on a plan item, if any (ignored while the kill switch is off)."""
    from app.config import settings  # noqa: PLC0415

    if not settings.song_filming_range_enabled:
        return None
    return parse_song_range(
        getattr(item, "song_range_start_s", None), getattr(item, "song_range_end_s", None)
    )


def _row_in_range(row: TakeAlignment, song_range: SongRange, cfg: Any) -> TakeAlignment:
    candidates = row.candidates_or_legacy()
    kept = [c for c in candidates if song_range.contains_start(c.delta_s)]
    if len(kept) == len(candidates):
        return row  # nothing outside the range: byte-identical row
    if not kept:
        # Nothing it could be inside the stretch: no evidence, instead of snapping to a far repeat.
        return TakeAlignment(
            media_id=row.media_id,
            proxy_generation=row.proxy_generation,
            status="unmatched",
            delta_s=None,
            confidence=min(row.confidence, 0.3),
            text_score=row.text_score,
            peak_z=row.peak_z,
            peak_ratio=row.peak_ratio,
        )
    ranked = sorted(kept, key=lambda c: -c.likelihood)
    best = ranked[0]
    margin = (
        1.0
        if len(ranked) == 1
        else min(1.0, max(0.0, (best.likelihood - ranked[1].likelihood) / best.likelihood))
    )
    uncertain = margin < float(cfg.song_align_ask_margin) or best.likelihood < float(
        cfg.song_align_ask_likelihood
    )
    has_match = (
        best.match_start_s is not None
        and best.match_end_s is not None
        and best.match_end_s > best.match_start_s
    )
    extra: dict[str, Any] = (
        {"match_start_s": best.match_start_s, "match_end_s": best.match_end_s} if has_match else {}
    )
    return TakeAlignment(
        media_id=row.media_id,
        proxy_generation=row.proxy_generation,
        status="ambiguous" if uncertain else "confident",
        delta_s=best.delta_s,
        confidence=min(1.0, max(0.0, best.likelihood)),
        text_score=row.text_score,
        peak_z=row.peak_z,
        peak_ratio=row.peak_ratio,
        alternates=(
            [AlignmentAlternate(delta_s=c.delta_s, score=c.likelihood) for c in ranked[:4]]
            if uncertain
            else []
        ),
        method=best.method if best.method in ("audio", "lyrics") else row.method,
        likelihood=min(1.0, max(0.0, best.likelihood)),
        margin=margin,
        candidates=[PlacementCandidate.model_validate(c.model_dump()) for c in ranked],
        **extra,
    )


def restrict_alignment_to_range(
    alignment: SongAlignment, song_range: SongRange | None, cfg: Any | None = None
) -> SongAlignment:
    """``alignment`` with every take's candidates cut to the creator's stretch of the song.

    ``None`` (no range) returns the alignment unchanged. A take left with no candidate inside the
    stretch becomes unmatched; one left with several is re-ranked and re-derived like the aligner
    does (margin, status, alternates), so everything downstream reads a consistent row.
    """
    if song_range is None:
        return alignment
    if cfg is None:
        from app.config import settings as cfg  # noqa: PLC0415
    rows = {
        media_id: _row_in_range(row, song_range, cfg) for media_id, row in alignment.takes.items()
    }
    if all(rows[m] is alignment.takes[m] for m in rows):
        return alignment
    return alignment.model_copy(update={"takes": rows})


# -- Validating and applying a range (PUT /song-range and the chat answer share it) ---------

# Shortest stretch worth matching inside: below this the creator filmed one line, not a part.
MIN_SONG_RANGE_S = 5.0


class SongRangeError(ValueError):
    """A range that cannot be applied. ``code`` is the wire error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


STALE_CODE = "song_range_stale"
INVALID_CODE = "song_range_invalid"


def normalized_range(
    start_s: float | None, end_s: float | None, song_duration_s: float | None
) -> tuple[float, float] | None:
    """``(start, end)`` to store, or ``None`` for "the whole song". Raises ``SongRangeError``.

    An end past the song is not an error (a creator rounding up): it is clamped to the song.
    """
    if start_s is None and end_s is None:
        return None
    if start_s is None or end_s is None:
        raise SongRangeError(INVALID_CODE, "Set both ends of the part you filmed, or neither.")
    song_s = float(song_duration_s or 0.0)
    if song_s and start_s >= song_s:
        raise SongRangeError(INVALID_CODE, "That starts after your song ends.")
    end = min(float(end_s), song_s) if song_s else float(end_s)
    if end - float(start_s) < MIN_SONG_RANGE_S:
        raise SongRangeError(INVALID_CODE, "Pick at least five seconds of your song.")
    return float(start_s), end


def apply_song_range(thread: Any, item: Any, value: tuple[float, float] | None) -> None:
    """Store ``value`` on the plan item and mirror it onto the thread's song media entry.

    The PlanItem is authoritative (the planner and the render worker read it); the thread entry is
    the projection the app shows. Both are cleared when ``value`` is ``None``.
    """
    item.song_range_start_s, item.song_range_end_s = value if value is not None else (None, None)
    state = dict(getattr(thread, "state", None) or {})
    media = [dict(entry) for entry in (state.get("media") or []) if isinstance(entry, dict)]
    for entry in media:
        if entry.get("role") != "song":
            continue
        if value is None:
            entry.pop("song_range", None)
        else:
            entry["song_range"] = {"start_s": value[0], "end_s": value[1]}
    state["media"] = media
    thread.state = state


# -- The question: "which part of the song did you film?" (KRI-588) -------------------------

# A take is "ambiguous between repeats" when it has two plausible places this far apart.
# Verses and choruses repeat tens of seconds apart; two candidates a few seconds apart are the
# same line.
REPEAT_APART_S = 15.0
# A candidate below this is noise, not a second place the take could really be.
REPEAT_MIN_LIKELIHOOD = 0.2

Events = Iterable[tuple[str, "dict[str, Any] | None"]]


def takes_ambiguous_between_repeats(alignment: SongAlignment, take_ids: Sequence[str]) -> list[str]:
    """Takes with two plausible placements far apart (a repeated verse), in the given order."""
    found: list[str] = []
    for media_id in take_ids:
        row = alignment.takes.get(media_id)
        if row is None:
            continue
        places = sorted(
            c.delta_s for c in row.candidates_or_legacy() if c.likelihood >= REPEAT_MIN_LIKELIHOOD
        )
        if len(places) >= 2 and places[-1] - places[0] >= REPEAT_APART_S:
            found.append(media_id)
    return found


def build_song_range_question(
    song_generation: int | None, song_duration_s: float | None
) -> SongRangeQuestion:
    return SongRangeQuestion(
        question_id=str(uuid.uuid4()),
        song_generation=song_generation,
        song_duration_s=(
            float(song_duration_s)
            if isinstance(song_duration_s, (int, float)) and song_duration_s > 0
            else None
        ),
    )


def song_range_question_text(question: SongRangeQuestion) -> str:
    """Self-sufficient copy (the app also renders the card)."""
    return say(
        en=(
            "Your song repeats its verses, and some of your clips could fit more than one place. "
            "Which part of the song did you film? Pick it below, or use the whole song."
        ),
        tr=(
            "Şarkın bölümlerini tekrar ediyor ve bazı klipler birden fazla yere uyabiliyor. "
            "Şarkının hangi kısmında çektin? Aşağıdan seç ya da tüm şarkıyı kullan."
        ),
    )


def parse_song_range_question(raw: Any) -> SongRangeQuestion | None:
    if not isinstance(raw, Mapping):
        return None
    try:
        return SongRangeQuestion.model_validate(dict(raw))
    except ValidationError:
        return None


@dataclass(frozen=True)
class SongRangeFold:
    """What the thread already did about the range question."""

    # Song generations a question was asked about. Asked once per song: whether the creator
    # answered, skipped or ignored it, the planner does not ask again (it would loop on a client
    # that cannot answer).
    asked_generations: frozenset[int | None] = frozenset()


def fold_song_range_events(events: Events) -> SongRangeFold:
    asked: set[int | None] = set()
    for role, payload in events:
        if role == "assistant" and isinstance(payload, Mapping):
            question = parse_song_range_question(payload.get(SONG_RANGE_QUESTION_KEY))
            if question is not None:
                asked.add(question.song_generation)
    return SongRangeFold(frozenset(asked))


def latest_open_song_range_question(events: Events) -> SongRangeQuestion | None:
    """The latest ``song_range_question`` not yet answered by a ``song_range`` answer."""
    latest: SongRangeQuestion | None = None
    for role, payload in events:
        if not isinstance(payload, Mapping):
            continue
        if role == "assistant":
            question = parse_song_range_question(payload.get(SONG_RANGE_QUESTION_KEY))
            if question is not None:
                latest = question
        elif role == "user" and isinstance(payload.get(SONG_RANGE_ANSWER_KEY), Mapping):
            answer = payload[SONG_RANGE_ANSWER_KEY]
            if latest is not None and answer.get("question_id") == latest.question_id:
                latest = None
    return latest


def validate_song_range_answer(
    answer: SongRangeAnswerIn, open_question: SongRangeQuestion | None
) -> tuple[float, float] | None:
    """The range to store (``None`` = whole song). Raises ``SongRangeError``."""
    if open_question is None or open_question.question_id != answer.question_id:
        raise SongRangeError(
            STALE_CODE, "That question is no longer open. Refresh and answer the latest one."
        )
    return normalized_range(answer.start_s, answer.end_s, open_question.song_duration_s)
