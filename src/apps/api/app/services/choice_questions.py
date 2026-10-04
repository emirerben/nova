"""Conflict-choice questions (KRI-282): detect, ask, remember, follow.

When the creator's instructions cannot all be honoured at once (or can be honoured in
more than one reasonable way), the assistant must not silently pick one. It asks ONE
focused question with concrete options (``choice_question`` on the assistant question
event), the app answers with ``choice_selection`` (SubmitTurnBody), and the answer is
replayed from the thread's own events on every later turn, so it is followed and never
asked again. Same event-replay design as ``clip_selection``: no extra state.

Adding a conflict later = build a :class:`ConflictCandidate` (a deterministic detector
here, or one the planner flags) and give its ``conflict_id`` a consumer for the chosen
option key. The first case is ``order_vs_group``: "chronological" together with "group
by sport" when the clips were filmed mixed together.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.services.clip_facts import order_by_capture_time

CHOICE_QUESTION_VERSION = 1
MAX_CHOICE_OPTIONS = 6
_NAME_MAX = 40

CONFLICT_ORDER_VS_GROUP = "order_vs_group"
OPT_GROUP_FIRST = "group_first"
OPT_CHRONOLOGICAL = "chronological"
ORDER_VS_GROUP_OPTIONS = (OPT_GROUP_FIRST, OPT_CHRONOLOGICAL)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChoiceSelectionIn(_Strict):
    """The app's answer to a ``choice_question`` (SubmitTurnBody.choice_selection)."""

    question_id: str = Field(min_length=1, max_length=64)
    option_key: str = Field(min_length=1, max_length=64)


@dataclass(frozen=True)
class ConflictOption:
    key: str
    label: str
    description: str | None = None
    recommended: bool = False


@dataclass(frozen=True)
class ConflictCandidate:
    """One detected (or planner-flagged) conflict, ready to be asked."""

    conflict_id: str
    reason: str
    intro: str
    options: tuple[ConflictOption, ...]


# ── Detection: ordering vs grouping ───────────────────────────────────────────


def _clean_name(name: str) -> str:
    return " ".join(str(name).split())[:_NAME_MAX]


def _join_names(names: Sequence[str]) -> str:
    shown = [_clean_name(n) for n in names if _clean_name(n)][:3]
    if len(shown) <= 1:
        return "".join(shown)
    return ", ".join(shown[:-1]) + " and " + shown[-1]


def split_groups(
    ordered_ids: Sequence[str], groups: Sequence[tuple[str, Sequence[str]]]
) -> list[tuple[str, int]]:
    """``(name, stretches)`` for every group that is NOT one contiguous stretch.

    A clip in several groups is ambiguous and belongs to none; clips in no group are
    ignored (they never break a group's stretch). Same rule as the montage receipt, so
    "is there a conflict" and "did the render group them" cannot disagree.
    """
    position = {media_id: i for i, media_id in enumerate(ordered_ids)}
    tally: dict[str, int] = {}
    for _name, members in groups:
        for media_id in set(members):
            tally[media_id] = tally.get(media_id, 0) + 1
    owned = sorted(position[m] for m, n in tally.items() if n == 1 and m in position)
    split: list[tuple[str, int]] = []
    for name, members in groups:
        mine = {position[m] for m in set(members) if tally.get(m) == 1 and m in position}
        stretches, inside = 0, False
        for index in owned:
            if index in mine and not inside:
                stretches += 1
            inside = index in mine
        if stretches > 1:
            split.append((name, stretches))
    return split


def detect_order_vs_group(
    *,
    wants_capture_order: bool,
    groups: Sequence[tuple[str, Sequence[str]]],
    clips: Sequence[tuple[str, datetime | None]],
    noun: str = "group",
) -> ConflictCandidate | None:
    """Chronological order AND >=2 groups that the filming order interleaves.

    ``groups`` are the resolved group intents as ``(name, member media ids)``;
    ``clips`` are ``(media id, capture time)`` in attachment order. No conflict when the
    creator did not ask for filming order, when there is no capture time to order by
    (the montage then keeps attachment order and nothing is traded off), or when the
    clips already form one block per group in capture order.
    """
    if not wants_capture_order:
        return None
    known = {media_id for media_id, _t in clips}
    live = [(n, [m for m in members if m in known]) for n, members in groups]
    live = [(n, members) for n, members in live if n and members]
    if len(live) < 2:
        return None
    times = {media_id: moment for media_id, moment in clips if moment is not None}
    ordering = order_by_capture_time([media_id for media_id, _t in clips], times)
    if ordering.basis != "capture_time":
        return None
    if not split_groups(ordering.ordered_ids, live):
        return None
    names = _join_names([n for n, _m in live])
    word = noun if noun else "group"
    return ConflictCandidate(
        conflict_id=CONFLICT_ORDER_VS_GROUP,
        reason=f"your {names} clips were filmed mixed together, so I can't do both.",
        intro=f"You asked for a chronological video and for the clips grouped by {word}.",
        options=(
            ConflictOption(
                key=OPT_GROUP_FIRST,
                label=f"Group by {word}, chronological inside each {word}",
                description=(
                    f"Each {word} plays as one block; blocks start in the order they first "
                    "appear, and clips inside a block keep the order you filmed them."
                ),
                recommended=True,
            ),
            ConflictOption(
                key=OPT_CHRONOLOGICAL,
                label=f"Keep it strictly chronological; {word}s may interleave",
                description="Clips play in the exact order you filmed them.",
            ),
        ),
    )


# ── Question payload ──────────────────────────────────────────────────────────


def build_choice_question(
    candidate: ConflictCandidate, *, question_id: str | None = None
) -> dict[str, Any]:
    """The additive ``choice_question`` payload for the assistant question event."""
    options = []
    for option in candidate.options[:MAX_CHOICE_OPTIONS]:
        row: dict[str, Any] = {
            "key": option.key,
            "label": option.label,
            "recommended": bool(option.recommended),
        }
        if option.description:
            row["description"] = option.description
        options.append(row)
    return {
        "version": CHOICE_QUESTION_VERSION,
        "question_id": question_id or str(uuid.uuid4()),
        # Replay key: which conflict this answers. Old/other clients ignore it.
        "conflict": candidate.conflict_id,
        "options": options,
        "allow_free_text": True,
    }


def choice_question_text(candidate: ConflictCandidate) -> str:
    """Self-sufficient plain text (old builds show only this)."""
    lines = [f"{candidate.intro} Unfortunately {candidate.reason} Which do you prefer?"]
    for index, option in enumerate(candidate.options[:MAX_CHOICE_OPTIONS], start=1):
        mark = " (recommended)" if option.recommended else ""
        lines.append(f"{index}. {option.label}{mark}")
    lines.append("Tap an option, or tell me in your own words.")
    return "\n".join(lines)


# ── Replaying thread events ───────────────────────────────────────────────────

Events = Iterable[tuple[str, "dict[str, Any] | None"]]


def _option_keys(question: Mapping[str, Any]) -> set[str]:
    return {
        str(o.get("key"))
        for o in question.get("options") or []
        if isinstance(o, Mapping) and o.get("key")
    }


def latest_open_choice_question(events: Events) -> dict[str, Any] | None:
    """The latest ``choice_question`` not yet answered by a ``choice_selection``.

    ``events`` are ``(role, payload)`` in chronological order.
    """
    latest: dict[str, Any] | None = None
    for role, payload in events:
        if not isinstance(payload, dict):
            continue
        if role == "assistant" and isinstance(payload.get("choice_question"), dict):
            latest = payload["choice_question"]
        elif role == "user" and isinstance(payload.get("choice_selection"), dict):
            if latest is not None and payload["choice_selection"].get("question_id") == latest.get(
                "question_id"
            ):
                latest = None
    return latest


def fold_choice_answers(events: Events) -> dict[str, str]:
    """``conflict_id -> chosen option key`` replayed from the thread (last answer wins).

    Only a selection that answers a question the server actually asked, with an option
    that question offered, counts.
    """
    questions: dict[str, dict[str, Any]] = {}
    answers: dict[str, str] = {}
    for role, payload in events:
        if not isinstance(payload, dict):
            continue
        if role == "assistant" and isinstance(payload.get("choice_question"), dict):
            question = payload["choice_question"]
            questions[str(question.get("question_id"))] = question
            continue
        selection = payload.get("choice_selection") if role == "user" else None
        if not isinstance(selection, dict):
            continue
        question = questions.get(str(selection.get("question_id")))
        conflict = question.get("conflict") if question else None
        if not conflict or selection.get("option_key") not in _option_keys(question):
            continue
        answers[str(conflict)] = str(selection["option_key"])
    return answers


__all__ = [
    "CHOICE_QUESTION_VERSION",
    "CONFLICT_ORDER_VS_GROUP",
    "OPT_CHRONOLOGICAL",
    "OPT_GROUP_FIRST",
    "ORDER_VS_GROUP_OPTIONS",
    "ChoiceSelectionIn",
    "ConflictCandidate",
    "ConflictOption",
    "build_choice_question",
    "choice_question_text",
    "detect_order_vs_group",
    "fold_choice_answers",
    "latest_open_choice_question",
    "split_groups",
]
