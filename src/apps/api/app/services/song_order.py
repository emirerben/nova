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

from app.kria.reply_language import say
from app.pipeline.take_assignment import (
    Assignment,
    TakeSpec,
    assign_takes,
    resolve_with_order,
    spec_from_alignment_row,
)
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


# A take's length is unknown to the alignment row; used only to size its claim when
# the manifest gave no duration and the aligner no matched range.
_DEFAULT_TAKE_S = 8.0
# No song length known: large enough never to clip a claim.
_UNBOUNDED_SONG_MS = 10**9


def _duration_s(row: TakeAlignment, given: float | None) -> float:
    if isinstance(given, (int, float)) and given > 0:
        return float(given)
    ends = [c.match_end_s for c in row.candidates_or_legacy() if c.match_end_s is not None]
    return max([*ends, _DEFAULT_TAKE_S])


def _specs(
    alignment: SongAlignment,
    media_ids: Sequence[str],
    durations: Mapping[str, float] | None,
) -> list[TakeSpec]:
    return [
        spec_from_alignment_row(
            m,
            _duration_s(_take_for(alignment, m), (durations or {}).get(m)),
            alignment.takes.get(m),
        )
        for m in dict.fromkeys(media_ids)
    ]


def _assignment_options() -> dict[str, Any]:
    from app.config import settings  # noqa: PLC0415

    return {
        "overlap_ms": int(round(settings.song_align_max_overlap_s * 1000)),
        "tie_ratio": settings.song_align_tie_ratio,
        "ask_likelihood": settings.song_align_ask_likelihood,
    }


def assign_for_question(
    alignment: SongAlignment,
    media_ids: Sequence[str],
    durations: Mapping[str, float] | None = None,
    song_duration_s: float | None = None,
) -> Assignment:
    """The deterministic assignment the question is derived from (no creator input)."""
    song_ms = (
        int(song_duration_s * 1000)
        if isinstance(song_duration_s, (int, float)) and song_duration_s > 0
        else _UNBOUNDED_SONG_MS
    )
    return assign_takes(_specs(alignment, media_ids, durations), song_ms, **_assignment_options())


def takes_needing_order(
    alignment: SongAlignment,
    media_ids: Sequence[str],
    durations: Mapping[str, float] | None = None,
    song_duration_s: float | None = None,
) -> list[str]:
    """Takes worth asking the creator about (KRI-471): the assignment's ``ask`` set.

    Placement is by likelihood, so a take is only asked about when the creator could
    usefully decide it: a tie (repeated chorus), a weak match, or no evidence at all.
    ``durations`` (media_id -> seconds, from the manifest) sizes each take's claim.
    """
    ask = assign_for_question(alignment, media_ids, durations, song_duration_s).ask
    return [m for m in dict.fromkeys(media_ids) if m in ask]


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
    durations: Mapping[str, float] | None = None,
    song_duration_s: float | None = None,
) -> SongOrderQuestion:
    """The question for ``media_ids`` (the current takes, in their stored order).

    ``proposed_order`` is the song order of the assigned positions. Takes with no
    evidence have no position: each goes right after the nearest preceding take (in
    capture order) that has one, the filming order being the best prior. They are
    sent as ``ambiguous`` with no ``song_start_s`` so the app lets the creator drag
    them (``reason="no_evidence"``); their alignment row stays unmatched.
    """
    unique = list(dict.fromkeys(media_ids))
    assignment = assign_for_question(alignment, unique, durations, song_duration_s)
    position: dict[str, float] = {m: c.delta_ms / 1000 for m, c in assignment.placed.items()}
    items: dict[str, SongOrderItem] = {}
    for media_id in unique:
        take = _take_for(alignment, media_id)
        claim = assignment.placed.get(media_id)
        reason = assignment.ask.get(media_id)
        if claim is not None:
            like = round(claim.likelihood, 4)
            items[media_id] = SongOrderItem(
                media_id=media_id,
                status="ambiguous" if reason else "confident",
                song_start_s=position[media_id],
                alternates=_alternates(take) if reason else [],
                likelihood=like,
                reason=reason,
            )
        else:
            # Unplaced with evidence (conflict) keeps its best guess for the card.
            cands = take.candidates_or_legacy()
            best = max(cands, key=lambda c: c.likelihood, default=None)
            items[media_id] = SongOrderItem(
                media_id=media_id,
                status="ambiguous",
                song_start_s=None,
                alternates=(
                    [AlignmentAlternate(delta_s=c.delta_s, score=c.likelihood) for c in cands][
                        :MAX_ALTERNATES_PER_ITEM
                    ]
                    if best is not None
                    else []
                ),
                likelihood=round(best.likelihood, 4) if best is not None else None,
                reason=reason or "no_evidence",
            )
    # Song order for takes with a position; the rest follow the nearest preceding
    # positioned take in capture order (or lead the list when none precedes them).
    positioned = sorted((m for m in unique if m in position), key=lambda m: (position[m], m))
    after: dict[str | None, list[str]] = {}
    previous: str | None = None
    for media_id in unique:
        if media_id in position:
            previous = media_id
        else:
            after.setdefault(previous, []).append(media_id)
    order: list[str] = list(after.get(None, []))
    for media_id in positioned:
        order.append(media_id)
        order.extend(after.get(media_id, []))
    ordered = [items[m] for m in order]
    return SongOrderQuestion(
        question_id=question_id or str(uuid.uuid4()),
        proposed_order=order,
        items=ordered,
        song_generation=(
            song_generation if song_generation is not None else alignment.song_generation
        ),
    )


def song_order_question_text(question: SongOrderQuestion) -> str:
    """Self-sufficient copy (the app also renders the video widgets)."""
    unsure = sum(1 for i in question.items if i.status == "ambiguous")
    if unsure == 1:
        return say(
            en=(
                "I'm not sure where one of your clips sits in the song. "
                "They're in the order I think; drag any that are out of place."
            ),
            tr=(
                "Kliplerinden birinin şarkıda nereye geldiğinden emin değilim. "
                "Klipleri doğru olduğunu düşündüğüm sırayla dizdim; yeri yanlış olanları sürükle."
            ),
        )
    return say(
        en=(
            f"I'm not sure where {unsure} of your clips sit in the song. "
            "They're in the order I think; drag any that are out of place."
        ),
        tr=(
            f"Kliplerinden {unsure} tanesinin şarkıda nereye geldiğinden emin değilim. "
            "Klipleri doğru olduğunu düşündüğüm sırayla dizdim; yeri yanlış olanları sürükle."
        ),
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


def resolve_uncertain_takes(
    alignment: SongAlignment,
    confirmed_order: Sequence[str],
    durations: Mapping[str, float] | None = None,
    song_duration_s: float | None = None,
    first_line_s: float | None = None,
) -> dict[str, dict[str, Any]]:
    """Positions for every take given the creator's confirmed order (KRI-471).

    Delegates to ``take_assignment.resolve_with_order``. Returns ``media_id -> row``
    in the creator's order, each row ``{"order_index", "delta_s" | None, "place":
    "pinned"|"stack"|"broll", "position_basis", "confirmed_by_creator",
    "likelihood", "status", "reason"?}``. ``confirmed_by_creator`` is True only when
    the creator's answer decided the position (``creator_position`` / ``creator_stack``);
    a take the aligner was sure about keeps its own position and ``aligner`` basis.
    """
    ids = list(dict.fromkeys(confirmed_order))
    song_ms = (
        int(song_duration_s * 1000)
        if isinstance(song_duration_s, (int, float)) and song_duration_s > 0
        else _UNBOUNDED_SONG_MS
    )
    choices = resolve_with_order(
        _specs(alignment, ids, durations),
        ids,
        song_ms,
        first_line_ms=int(round((first_line_s or 0.0) * 1000)),
        **_assignment_options(),
    )
    out: dict[str, dict[str, Any]] = {}
    for media_id, choice in choices.items():
        row: dict[str, Any] = {
            "order_index": choice.order_index,
            "delta_s": None if choice.delta_ms is None else choice.delta_ms / 1000,
            "place": choice.place,
            "position_basis": choice.basis,
            "confirmed_by_creator": choice.confirmed,
            "likelihood": round(choice.likelihood, 4),
            "status": "unmatched" if choice.delta_ms is None else "confident",
        }
        if choice.reason:
            row["reason"] = choice.reason
        out[media_id] = row
    return out


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def apply_resolved_song_takes(
    alignment: SongAlignment, resolved_takes: Sequence[Mapping[str, Any]] | None
) -> tuple[SongAlignment, list[str], dict[str, dict[str, Any]]]:
    """Read the strategy's ``resolved_song_takes`` for the planner.

    Returns ``(alignment, confirmed_order, creator_choices)``. The alignment is
    returned UNCHANGED (rows are no longer rewritten to ambiguous/unmatched);
    ``creator_choices`` maps media_id -> ``{"delta_s" | None, "place",
    "position_basis", "confirmed_by_creator"}`` for the takes the creator's answer
    placed:

    * a row with ``place`` ("pinned"/"stack"/"broll") is taken as written;
    * a legacy row (no ``place``) that the creator confirmed keeps its old meaning:
      ``delta_s`` pins it (``creator_position``), ``delta_s None`` is B-roll;
    * a legacy row the creator did not confirm is left to the assignment.
    """
    order: list[str] = []
    choices: dict[str, dict[str, Any]] = {}
    for row in sorted(
        (r for r in resolved_takes or () if isinstance(r, Mapping)),
        key=lambda r: r.get("order_index") if isinstance(r.get("order_index"), int) else 10**6,
    ):
        media_id = str(row.get("media_id") or "")
        if not media_id or media_id in order or media_id not in alignment.takes:
            continue
        order.append(media_id)
        delta = _finite(row.get("delta_s"))
        place = row.get("place")
        if place not in ("pinned", "stack", "broll"):
            if not row.get("confirmed_by_creator"):
                continue
            place = "pinned" if delta is not None else "broll"
            basis = "creator_position"
        else:
            if place != "broll" and delta is None:
                place = "broll"
            basis = row.get("position_basis") or (
                "creator_stack" if place == "stack" else "creator_position"
            )
        choices[media_id] = {
            "delta_s": None if place == "broll" else delta,
            "place": place,
            "position_basis": basis,
            "confirmed_by_creator": bool(row.get("confirmed_by_creator")),
            "reason": row.get("reason"),
        }
    return alignment, order, choices


def resolved_song_takes_payload(resolved: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The server-owned ``resolved_song_takes`` list written onto the strategy, in the
    creator's confirmed order:

    ``[{"media_id", "order_index", "delta_s": float | None, "place":
    "pinned"|"stack"|"broll", "position_basis", "confirmed_by_creator", "likelihood",
    "status": "confident"|"unmatched", "reason"?}, ...]``.
    ``place == "broll"`` (``delta_s is None``) means "muted B-roll, never placed by
    song time"; ``"stack"`` is laid by the creator's order (approximate sync).
    """
    out: list[dict[str, Any]] = []
    for index, (media_id, take) in enumerate(resolved.items()):
        delta = take.get("delta_s")
        item: dict[str, Any] = {
            "media_id": media_id,
            "order_index": take.get("order_index", index),
            "delta_s": delta,
            "status": take.get("status", "unmatched" if delta is None else "confident"),
            "confirmed_by_creator": bool(take.get("confirmed_by_creator")),
        }
        if take.get("place"):
            item["place"] = take["place"]
        if take.get("position_basis"):
            item["position_basis"] = take["position_basis"]
        if take.get("likelihood") is not None:
            item["likelihood"] = take["likelihood"]
        if take.get("reason"):
            item["reason"] = take["reason"]
        out.append(item)
    return out
