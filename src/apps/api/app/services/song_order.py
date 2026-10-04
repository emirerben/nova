"""Uncertain-takes question for lip-sync song montages (KRI-374 lane E).

When the aligner cannot place a take confidently (a repeated chorus, a silent or
noisy take), the creator is shown the takes as video widgets in a PROPOSED order
and confirms or reorders them. The server never places an uncertain take at a
guessed time: it resolves to the alternate whose song position fits between the
creator's confirmed neighbours, or the take becomes muted B-roll.

Modeled on ``services/clip_selection.py``: everything is derived from the
thread's own events (the assistant event carrying ``song_order_question`` and the
user event carrying ``song_order``), so no extra state is stored.

Time convention (``schemas/user_song.py``): ``song_time = take_time + delta_s``,
so a take's *song position* is its ``delta_s``.

Everything here is pure; the planner and runtime own the I/O.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from app.schemas.user_song import (
    SONG_ALIGNMENT_VERSION,
    SONG_ORDER_ANSWER_KEY,
    SONG_ORDER_QUESTION_KEY,
    AlignmentAlternate,
    SongAlignment,
    SongOrderAnswerIn,
    SongOrderItem,
    SongOrderQuestion,
    TakeAlignment,
)

MAX_ALTERNATES_PER_ITEM = 4
# Two song positions closer than this are the same position.
_POSITION_EPS_S = 1e-3

# Event-payload keys of the OTHER structured chat answers (KRI-282 clip picker /
# conflict choice). Tapped answers, never free text: see `thread_keeps_lipsync`.
_STRUCTURED_ANSWER_KEYS = ("choice_selection", "clip_selection")

STALE_CODE = "song_order_stale"
INVALID_CODE = "song_order_invalid"


class SongOrderError(ValueError):
    """A creator answer that cannot be applied. ``code`` is the wire error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# -- Reading alignment rows ----------------------------------------------------


def _unmatched(media_id: str) -> TakeAlignment:
    return TakeAlignment(media_id=media_id, status="unmatched")


def _take_for(alignment: SongAlignment, media_id: str) -> TakeAlignment:
    return alignment.takes.get(media_id) or _unmatched(media_id)


def uncertain_media_ids(alignment: SongAlignment, media_ids: Sequence[str]) -> list[str]:
    """Takes that are not confidently placed (a missing row counts as unmatched)."""
    return [m for m in media_ids if _take_for(alignment, m).status != "confident"]


def load_ready_alignment(
    raw_alignment: Any,
    *,
    media_ids: Sequence[str],
    song_generation: int | None = None,
    raw_analysis: Any = None,
) -> SongAlignment | None:
    """The PlanItem's stored alignment if it is usable now, else ``None`` (= still pending).

    Usable means: parses, current alignment version, same song generation as the
    PlanItem, and a row for EVERY take. A failed song analysis (nothing will ever
    arrive) yields an all-unmatched alignment so the creator can still order the
    takes instead of waiting forever.
    """
    alignment: SongAlignment | None = None
    if isinstance(raw_alignment, SongAlignment):
        alignment = raw_alignment
    elif isinstance(raw_alignment, Mapping):
        try:
            alignment = SongAlignment.model_validate(raw_alignment)
        except ValidationError:
            alignment = None
    if alignment is not None:
        stale = (
            alignment.version != SONG_ALIGNMENT_VERSION
            or (song_generation is not None and alignment.song_generation != song_generation)
            or any(m not in alignment.takes for m in media_ids)
        )
        if not stale:
            return alignment
    status = (
        raw_analysis.get("status")
        if isinstance(raw_analysis, Mapping)
        else getattr(raw_analysis, "status", None)
    )
    if status == "failed":
        return SongAlignment(
            song_generation=song_generation if song_generation is not None else 0,
            takes={m: _unmatched(m) for m in media_ids},
        )
    return None


# -- Question construction -----------------------------------------------------


def _song_start(take: TakeAlignment) -> float | None:
    if take.status == "unmatched":
        return None
    return take.delta_s


def _alternates(take: TakeAlignment) -> list[AlignmentAlternate]:
    if take.status == "confident":
        return []
    return list(take.alternates)[:MAX_ALTERNATES_PER_ITEM]


def build_song_order_question(
    alignment: SongAlignment,
    media_ids: Sequence[str],
    *,
    question_id: str | None = None,
    song_generation: int | None = None,
) -> SongOrderQuestion:
    """The question for ``media_ids`` (the current takes, in their stored order).

    ``proposed_order``: earliest song position first. Confident takes carry their
    ``song_start_s``; ambiguous ones carry their best guess plus ``alternates``;
    unmatched ones carry neither and sort last (stable, original order).
    """
    unique = list(dict.fromkeys(media_ids))
    items = [
        SongOrderItem(
            media_id=media_id,
            status=take.status,
            song_start_s=_song_start(take),
            alternates=_alternates(take),
        )
        for media_id in unique
        for take in (_take_for(alignment, media_id),)
    ]
    placed = sorted(
        (i for i in items if i.song_start_s is not None), key=lambda i: i.song_start_s or 0.0
    )
    unplaced = [i for i in items if i.song_start_s is None]
    ordered = placed + unplaced
    return SongOrderQuestion(
        question_id=question_id or str(uuid.uuid4()),
        proposed_order=[i.media_id for i in ordered],
        items=ordered,
        song_generation=(
            song_generation if song_generation is not None else alignment.song_generation
        ),
    )


def song_order_question_text(question: SongOrderQuestion) -> str:
    """Self-sufficient copy (the app also renders the video widgets)."""
    uncertain = sum(1 for i in question.items if i.status != "confident")
    if uncertain == 1:
        return (
            "I couldn't tell where one of your takes sits in the song. "
            "Preview them in this order and drag any that are out of place."
        )
    return (
        f"I couldn't tell where {uncertain} of your takes sit in the song. "
        "Preview them in this order and drag any that are out of place."
    )


# -- Thread events -------------------------------------------------------------

Event = tuple[str, "dict[str, Any] | None"]


def _parse_question(raw: Any) -> SongOrderQuestion | None:
    if not isinstance(raw, dict):
        return None
    try:
        return SongOrderQuestion.model_validate(raw)
    except ValidationError:
        return None


def _same_generation(question: SongOrderQuestion, song_generation: int | None) -> bool:
    """A question asked before generations were recorded (``None``) is trusted as before."""
    return (
        song_generation is None
        or question.song_generation is None
        or question.song_generation == song_generation
    )


def latest_open_song_order_question(events: Iterable[Event]) -> SongOrderQuestion | None:
    """The latest ``song_order_question`` not yet answered by a ``song_order``.

    ``events`` are ``(role, payload)`` in chronological order.
    """
    latest: SongOrderQuestion | None = None
    for role, payload in events:
        if not isinstance(payload, dict):
            continue
        if role == "assistant":
            question = _parse_question(payload.get(SONG_ORDER_QUESTION_KEY))
            if question is not None:
                latest = question
        elif role == "user" and isinstance(payload.get(SONG_ORDER_ANSWER_KEY), dict):
            if latest is not None and (
                payload[SONG_ORDER_ANSWER_KEY].get("question_id") == latest.question_id
            ):
                latest = None
    return latest


@dataclass(frozen=True)
class SongOrderFold:
    """The standing creator-confirmed order (latest answer wins)."""

    question_id: str | None = None
    ordered_media_ids: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.ordered_media_ids)

    def covers(self, media_ids: Iterable[str]) -> bool:
        """True when the answer ordered exactly the current takes (a new or removed
        take invalidates it, so the planner asks again instead of guessing)."""
        return set(self.ordered_media_ids) == set(media_ids)


def fold_song_orders(events: Iterable[Event], song_generation: int | None = None) -> SongOrderFold:
    """Replay question/answer events (chronological) into the standing order.

    Only answers that match a question the server really asked, and that ordered
    exactly that question's takes, count; the latest such answer wins. With
    ``song_generation`` given, an answer to a question asked about another song
    generation is ignored, so a replaced song re-asks instead of reusing an order
    the creator confirmed against different audio.
    """
    questions: dict[str, SongOrderQuestion] = {}
    folded = SongOrderFold()
    for role, payload in events:
        if not isinstance(payload, dict):
            continue
        if role == "assistant":
            question = _parse_question(payload.get(SONG_ORDER_QUESTION_KEY))
            if question is not None:
                questions[question.question_id] = question
            continue
        answer = payload.get(SONG_ORDER_ANSWER_KEY) if role == "user" else None
        if not isinstance(answer, dict):
            continue
        question = questions.get(str(answer.get("question_id")))
        ordered = answer.get("ordered_media_ids")
        if question is None or not isinstance(ordered, list):
            continue
        if len(set(ordered)) != len(ordered) or set(ordered) != set(question.proposed_order):
            continue
        if not _same_generation(question, song_generation):
            continue
        folded = SongOrderFold(question.question_id, tuple(str(m) for m in ordered))
    return folded


def thread_keeps_lipsync(events: Iterable[Event], song_generation: int | None) -> bool:
    """True when the thread is mid-way through the song-order exchange for this song.

    That is: a question for the current song generation is still open, or the
    latest user event IS the answer to such a question (the turn being planned is
    the answer turn). The Main Creator model re-runs on that turn and may now say
    ``song_sync != "lipsync"``; the planner keeps lip-sync so the answer is applied
    instead of silently dropped.
    """
    rows = list(events)
    questions: dict[str, SongOrderQuestion] = {}
    for role, payload in rows:
        if role == "assistant" and isinstance(payload, dict):
            question = _parse_question(payload.get(SONG_ORDER_QUESTION_KEY))
            if question is not None:
                questions[question.question_id] = question
    open_question = latest_open_song_order_question(rows)
    if open_question is not None and _same_generation(open_question, song_generation):
        return True
    for role, payload in reversed(rows):
        if role != "user":
            continue
        answer = payload.get(SONG_ORDER_ANSWER_KEY) if isinstance(payload, dict) else None
        if not isinstance(answer, dict):
            if isinstance(payload, dict) and any(k in payload for k in _STRUCTURED_ANSWER_KEYS):
                # A tapped answer to ANOTHER server question (KRI-282 choice / clip
                # picker) asked after the song order was confirmed: it isn't a free-text
                # request to change the edit, so keep looking for the song answer.
                continue
            return False  # the latest user turn is something else
        question = questions.get(str(answer.get("question_id")))
        return question is not None and _same_generation(question, song_generation)
    return False


def validate_song_order_answer(
    answer: SongOrderAnswerIn, open_question: SongOrderQuestion | None
) -> None:
    """Raise ``SongOrderError`` unless ``answer`` answers the latest open question exactly."""
    if open_question is None or open_question.question_id != answer.question_id:
        raise SongOrderError(
            STALE_CODE, "That order question is no longer open. Refresh and answer the latest one."
        )
    ids = answer.ordered_media_ids
    if len(set(ids)) != len(ids):
        raise SongOrderError(INVALID_CODE, "That order lists the same take more than once.")
    if set(ids) != set(open_question.proposed_order):
        raise SongOrderError(
            INVALID_CODE, "That order must include exactly the takes I asked about."
        )


# -- Resolving uncertain takes -------------------------------------------------


def _candidates(take: TakeAlignment) -> list[tuple[float, float]]:
    """``(delta_s, rank_score)`` for every position this take could sit at.

    The aligner's primary ``delta_s`` ranks first on ties (it is the strongest peak);
    alternates rank by their own score.
    """
    alts = [(a.delta_s, a.score) for a in take.alternates]
    out: list[tuple[float, float]] = []
    if take.delta_s is not None:
        top = max([s for _d, s in alts], default=1.0)
        out.append((take.delta_s, top))
    for delta, score in alts:
        if not any(abs(delta - d) <= _POSITION_EPS_S for d, _s in out):
            out.append((delta, score))
    return [(d, s) for d, s in out if math.isfinite(d)]


def resolve_uncertain_takes(
    alignment: SongAlignment, confirmed_order: Sequence[str]
) -> dict[str, dict[str, Any]]:
    """Pin every take for a creator-confirmed order.

    Returns ``media_id -> {"delta_s", "status", "confirmed_by_creator"}`` (the
    ``UserSongTake`` shape):

    * confident take  -> its own ``delta_s``, ``confirmed_by_creator=False``;
    * uncertain take  -> the candidate position (primary or alternate) that lies
      strictly between its confirmed neighbours in ``confirmed_order``, best score
      first; ``status="confident"``, ``confirmed_by_creator=True``;
    * no fitting candidate -> B-roll: ``delta_s=None``, ``status="unmatched"``,
      ``confirmed_by_creator=True``. Never a guessed position.

    Uncertain takes resolve left to right, so one resolved to position X bounds the
    next. The lower bound is the nearest earlier take with a known position, the
    upper bound the nearest later CONFIDENT take.
    """
    resolved: dict[str, dict[str, Any]] = {}
    takes = [_take_for(alignment, m) for m in dict.fromkeys(confirmed_order)]
    known: list[float | None] = [t.delta_s if t.status == "confident" else None for t in takes]
    for index, take in enumerate(takes):
        if take.status == "confident":
            resolved[take.media_id] = {
                "delta_s": take.delta_s,
                "status": "confident",
                "confirmed_by_creator": False,
            }
            continue
        lo = next((k for k in reversed(known[:index]) if k is not None), -math.inf)
        hi = next(
            (
                t.delta_s
                for t in takes[index + 1 :]
                if t.status == "confident" and t.delta_s is not None
            ),
            math.inf,
        )
        fitting = [
            (d, s) for d, s in _candidates(take) if lo + _POSITION_EPS_S < d < hi - _POSITION_EPS_S
        ]
        if fitting:
            best = max(fitting, key=lambda c: (c[1], -c[0]))[0]
            known[index] = best
            resolved[take.media_id] = {
                "delta_s": best,
                "status": "confident",
                "confirmed_by_creator": True,
            }
        else:
            resolved[take.media_id] = {
                "delta_s": None,
                "status": "unmatched",
                "confirmed_by_creator": True,
            }
    return resolved


def apply_resolved_song_takes(
    alignment: SongAlignment, resolved_takes: Sequence[Mapping[str, Any]] | None
) -> tuple[SongAlignment, list[str]]:
    """Fold the strategy's ``resolved_song_takes`` back onto an alignment for the planner.

    ``resolved_song_takes`` (written by the planner's song-order gate after the
    creator answered) says, per take in the creator's confirmed order, where it sits
    (``delta_s``) or that it is B-roll (``delta_s is None``). The lip-sync planner
    places an uncertain take only when it is in ``confirmed_order``, and only at one
    of the take's own candidate positions, so:

    * a creator-confirmed take with a position keeps ONLY that position (status
      ``ambiguous`` so the planner treats it as the confirmed uncertain take it is);
    * a creator-confirmed take with no position becomes ``unmatched`` (B-roll);
    * every other row is left exactly as aligned (a confident take stays confident,
      a take the creator was never asked about stays unplaceable).

    Returns ``(alignment, confirmed_order)``. Nothing here invents a position.
    """
    order: list[str] = []
    takes = dict(alignment.takes)
    for row in resolved_takes or ():
        media_id = str(row.get("media_id") or "")
        if not media_id or media_id in order or media_id not in takes:
            continue
        order.append(media_id)
        if not row.get("confirmed_by_creator"):
            continue
        current = takes[media_id]
        delta = row.get("delta_s")
        if isinstance(delta, (int, float)) and not isinstance(delta, bool) and math.isfinite(delta):
            takes[media_id] = current.model_copy(
                update={"status": "ambiguous", "delta_s": float(delta), "alternates": []}
            )
        else:
            takes[media_id] = current.model_copy(
                update={"status": "unmatched", "delta_s": None, "alternates": []}
            )
    return alignment.model_copy(update={"takes": takes}), order


def resolved_song_takes_payload(resolved: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The server-owned ``resolved_song_takes`` list written onto the strategy:

    ``[{"media_id": str, "delta_s": float | None, "status": "confident"|"unmatched",
    "confirmed_by_creator": bool}, ...]`` in the creator's confirmed order.
    ``delta_s is None`` (status ``unmatched``) means "use as B-roll, never place by song time".
    """
    return [
        {
            "media_id": media_id,
            "delta_s": take.get("delta_s"),
            "status": take.get("status", "unmatched"),
            "confirmed_by_creator": bool(take.get("confirmed_by_creator")),
        }
        for media_id, take in resolved.items()
    ]
