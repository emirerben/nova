"""A part of one clip the creator asked never to show, settled per chat turn (Leverkusen M2).

"The kids at the start of the second goal clip are another family's children, so don't show
them", then "keep that clip, just start it after the kids (after about 2.5 seconds)". The
planner writes a ``hide_part`` intent (the clip, the edge, the creator's seconds) and the
resolver finds the clip (picker when unsure). This module finishes the turn's part of it:

* carry: the whole request is re-planned every turn by a model, so a part the head draft
  already hid can come back missing, without its seconds, or with fewer. It is kept, with
  the most seconds ever asked for that clip and edge (``carry_hidden_parts``);
* settle: a part with no edge or no seconds, or one that leaves nothing of its clip, is a
  question for the creator (``hidden_part_question``), never a guess and never a render
  with the part showing.

The seconds are the creator's own number. A clip record's moment times are approximate
(Gemini segment times) and nothing frame-checks them, so they never decide which frames are
hidden; when the creator gave no number, Kria asks.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.kria.reply_language import say
from app.pipeline.hidden_parts import HIDDEN_EDGE_GUARD_S, MIN_SHOWN_S
from app.schemas.clip_intents import ResolvedClipIntent

# Evidence on an assignment kept from the head draft (same idea as KRI-761's exclusions).
EARLIER_DRAFT_EVIDENCE = "earlier draft"


def earlier_hidden_parts(snapshot: object) -> list[ResolvedClipIntent]:
    """The settled ``hide_part`` intents a draft snapshot carries; malformed rows read as none."""
    strategy = snapshot.get("strategy") if isinstance(snapshot, dict) else None
    rows = strategy.get("resolved_clip_intents") if isinstance(strategy, dict) else None
    out: list[ResolvedClipIntent] = []
    for row in rows if isinstance(rows, list) else ():
        if not isinstance(row, dict) or row.get("op") != "hide_part":
            continue
        try:
            intent = ResolvedClipIntent.model_validate(row)
        except ValueError:  # a pydantic ValidationError: skip the row, keep the turn
            continue
        if (
            intent.status == "resolved"
            and intent.assignments
            and intent.part_s
            and intent.part_edge
        ):
            out.append(intent)
    return out


def _keys(intent: ResolvedClipIntent) -> set[tuple[str, str]]:
    return {(a.media_id, str(intent.part_edge)) for a in intent.assignments}


def carry_hidden_parts(
    intents: Sequence[ResolvedClipIntent],
    earlier: Sequence[ResolvedClipIntent],
    *,
    clip_ids: set[str],
) -> tuple[list[ResolvedClipIntent], int]:
    """``intents`` with every part the head draft hid still hidden. Returns (intents, carried).

    For each settled earlier part (clip and edge) still attached: this turn's matching part
    gets the larger number of seconds, or the seconds when it has none; when this turn has
    no part for that clip and edge at all, the earlier intent comes back as it was. A part
    is never shrunk or dropped by a re-plan: withdrawing it takes the creator's own words,
    which this step cannot read yet.
    """
    out = list(intents)
    carried = 0
    for before in earlier:
        live = {(media_id, edge) for media_id, edge in _keys(before) if media_id in clip_ids}
        if not live:
            continue
        matched = False
        for index, intent in enumerate(out):
            if intent.op != "hide_part" or intent.status != "resolved":
                continue
            if not live & _keys(intent):
                continue
            matched = True
            seconds = max(intent.part_s or 0.0, before.part_s or 0.0)
            if seconds != intent.part_s:
                out[index] = intent.model_copy(update={"part_s": seconds})
                carried += 1
        if matched:
            continue
        assignments = [
            a.model_copy(update={"evidence": EARLIER_DRAFT_EVIDENCE})
            for a in before.assignments
            if a.media_id in clip_ids
        ]
        taken = {intent.intent_id for intent in out}
        intent_id = (
            before.intent_id if before.intent_id not in taken else f"{before.intent_id}-kept"
        )
        out.append(
            before.model_copy(update={"assignments": assignments, "intent_id": intent_id[:40]})
        )
        carried += 1
    return out, carried


def _clip_name(media_id: str, clips: Sequence[Any]) -> str:
    for clip in clips:
        if getattr(clip, "media_id", None) == media_id:
            from app.services.clip_file_names import clip_display_name  # noqa: PLC0415

            name = clip_display_name(getattr(clip, "file_name", None))
            if name:
                return name
    return say(en="that clip", tr="o klip")


def _seconds(value: float) -> str:
    return f"{value:g}"


def hidden_part_question(intents: Sequence[ResolvedClipIntent], clips: Sequence[Any]) -> str | None:
    """The one question a settled-membership ``hide_part`` still needs, or None.

    Asked about the first part that has a clip but no edge or no seconds, or that would
    leave less than a moment of its clip. A part the creator matched to no clip (a picker
    "None") hides nothing and asks nothing.
    """
    durations: Mapping[str, float | None] = {
        str(getattr(clip, "media_id", "")): getattr(clip, "duration_s", None) for clip in clips
    }
    for intent in intents:
        if intent.op != "hide_part" or intent.status != "resolved" or not intent.assignments:
            continue
        name = _clip_name(intent.assignments[0].media_id, clips)
        what = intent.part_attribute or say(en="that part", tr="o bölüm")
        if intent.part_edge is None:
            return say(
                en=(
                    f"I can keep a part out only at the start or the end of a clip. Is "
                    f"{what} at the start or the end of {name}, and for how many seconds? "
                    "Or tell me to leave the whole clip out."
                ),
                tr=(
                    f"Bir klipten yalnızca başındaki ya da sonundaki bir bölümü çıkarabiliyorum. "
                    f"{what} {name} klibinin başında mı sonunda mı, kaç saniye sürüyor? "
                    "Ya da klibin tamamını çıkarmamı söyle."
                ),
            )
        if intent.part_s is None:
            if intent.part_edge == "start":
                return say(
                    en=(
                        f"How many seconds at the start of {name} show {what}? I'll start that "
                        "clip right after them, or tell me to leave the whole clip out."
                    ),
                    tr=(
                        f"{name} klibinin başındaki kaç saniyede {what} görünüyor? Klibi hemen "
                        "sonrasından başlatırım; ya da klibin tamamını çıkarmamı söyle."
                    ),
                )
            return say(
                en=(
                    f"How many seconds at the end of {name} show {what}? I'll end that clip "
                    "just before them, or tell me to leave the whole clip out."
                ),
                tr=(
                    f"{name} klibinin sonundaki kaç saniyede {what} görünüyor? Klibi hemen "
                    "öncesinde bitiririm; ya da klibin tamamını çıkarmamı söyle."
                ),
            )
        for assignment in intent.assignments:
            length = durations.get(assignment.media_id)
            if length is None:
                continue
            if length - intent.part_s - HIDDEN_EDGE_GUARD_S < MIN_SHOWN_S:
                name = _clip_name(assignment.media_id, clips)
                return say(
                    en=(
                        f"{name} is only {_seconds(round(length, 1))} seconds long, so leaving "
                        f"out {_seconds(intent.part_s)} seconds leaves nothing of it to show. "
                        "Should I leave the whole clip out?"
                    ),
                    tr=(
                        f"{name} yalnızca {_seconds(round(length, 1))} saniye; "
                        f"{_seconds(intent.part_s)} saniyesini çıkarınca gösterecek bir şey "
                        "kalmıyor. Klibin tamamını çıkarayım mı?"
                    ),
                )
    return None


__all__ = [
    "EARLIER_DRAFT_EVIDENCE",
    "carry_hidden_parts",
    "earlier_hidden_parts",
    "hidden_part_question",
]
