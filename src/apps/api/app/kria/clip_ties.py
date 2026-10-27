"""A Voiceover shot two footage clips match equally well is the creator's to pick (KRI-962).

Prod thread 99627FBC (Don Julio walk-in N1): "the DON JULIO sign while I say 'Don Julio'"
matched the window-sign clip AND Atakan at the "PARRILLA DON JULIO" plaque, both at 0.9.
Both really show a Don Julio sign, so nothing recorded could say which one the creator
meant, and nothing asked: the alignment agent opened on Atakan (no sign on screen at "Don
Julio") and put the window sign on the unrelated last line.

In a Voiceover edit each named shot sits on one moment of the creator's voice, so a tie is
a real choice. This module finds those ties and asks with the same clip picker the other
asks use, the tied clips pre-ticked:

* ``voiceover_ties``: each resolved ``include`` whose top-confidence footage matches are two
  or more videos that no other ask needs. A video is needed elsewhere when it is the only
  footage match of another ask (N1's "the grill" matched three clips at 0.9, but one is the
  only flowers clip and one the only butcher clip: one is left, nothing to ask). Photos
  never count (a Visuals photo is a card, not the shot), a clip the creator left out never
  counts, an include matching every footage clip is the whole set, not one shot, and an ask
  the creator already answered (a tap, a typed file name) is never asked.
* ``tie_question`` / ``with_tie_categories``: the picker for them, alone or added to the
  card the turn already asks (one card, one turn, as KRI-807 does for skips).

Order, group and caption asks are never ties: they place or name every clip they match. The
answer rides ``fold_clip_selections`` like every picker answer, so the tapped clips settle the
shot on later turns (``apply_creator_selections``) and it is never asked again. A shot the
creator keeps both clips for is placed by the narrated alignment, as before.

Nothing here reads creator words: the matches are the resolver's, the answers the creator's.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from app.kria.reply_language import say
from app.schemas.clip_intents import CREATOR_EVIDENCE, ResolvedClipIntent
from app.services.clip_selection import (
    MAX_QUESTION_CLIPS,
    build_clip_question,
    category_label,
    intent_key,
)

# Resolver confidences are coarse (0.8, 0.9, 1.0): equal within float noise is a tie.
_TIE_EPSILON = 1e-6


class _Clip(Protocol):
    media_id: str
    kind: str


@dataclass(frozen=True)
class TiedShot:
    intent: ResolvedClipIntent
    # The tied videos no other ask needs, in clip order (two or more).
    media_ids: tuple[str, ...]


def left_out(intents: Iterable[ResolvedClipIntent]) -> set[str]:
    """Clips a resolved ``exclude`` leaves out of the edit."""
    return {
        a.media_id
        for intent in intents
        if intent.op == "exclude" and intent.status == "resolved"
        for a in intent.assignments
    }


def voiceover_ties(intents: Sequence[ResolvedClipIntent], clips: Sequence[_Clip]) -> list[TiedShot]:
    """The named shots of a Voiceover edit two or more free footage clips match equally."""
    order = {c.media_id: i for i, c in enumerate(clips) if c.kind == "video"}
    skipped = left_out(intents)

    def footage(intent: ResolvedClipIntent) -> list[Any]:
        return [a for a in intent.assignments if a.media_id in order and a.media_id not in skipped]

    live = [i for i in intents if i.status == "resolved" and i.op != "exclude"]
    only_match = {id(i): {a.media_id for a in footage(i)} for i in live}
    ties: list[TiedShot] = []
    for intent in live:
        if intent.op != "include" or intent.label_source != "clip":
            continue
        if any(a.evidence in CREATOR_EVIDENCE for a in intent.assignments):
            continue
        matches = footage(intent)
        if len(matches) < 2 or {a.media_id for a in matches} >= order.keys() - skipped:
            # One match is no choice; every footage clip is the whole set ("all five
            # footage clips" on the Troia museum job), never one shot.
            continue
        top = max(a.confidence for a in matches)
        tied = {a.media_id for a in matches if top - a.confidence < _TIE_EPSILON}
        needed = {
            media_id
            for other in live
            if other is not intent and len(only_match[id(other)]) == 1
            for media_id in only_match[id(other)]
        }
        free = sorted(tied - needed, key=order.__getitem__)
        if len(free) >= 2:
            ties.append(TiedShot(intent=intent, media_ids=tuple(free)))
    return ties


def _question_text(asked: Sequence[tuple[str, int]]) -> str:
    """``asked``: (label, number of tied clips) per row the card kept."""
    if len(asked) == 1:
        label, count = asked[0]
        if count == 2:
            return say(
                en=(
                    f'Two of your clips show "{label}". Which one should I use for it? '
                    "Keep that one ticked and untick the other."
                ),
                tr=(
                    f'"{label}" iki klibinde de görünüyor. Hangisini kullanayım? '
                    "Onu işaretli bırak, diğerinin işaretini kaldır."
                ),
            )
        return say(
            en=(
                f'{count} of your clips show "{label}". Which one should I use for it? '
                "Keep that one ticked and untick the others."
            ),
            tr=(
                f'"{label}" {count} klibinde görünüyor. Hangisini kullanayım? '
                "Onu işaretli bırak, diğerlerinin işaretini kaldır."
            ),
        )
    shown = ", ".join(f'"{label}"' for label, _count in asked[:4])
    more = say(
        en=f" and {len(asked) - 4} more" if len(asked) > 4 else "",
        tr=f" ve {len(asked) - 4} tane daha" if len(asked) > 4 else "",
    )
    return say(
        en=(
            f"More than one of your clips shows {shown}{more}. "
            "For each one, keep the clip to use ticked and untick the others."
        ),
        tr=(
            f"Şunlar birden fazla klibinde görünüyor: {shown}{more}. "
            "Her biri için kullanılacak klibi işaretli bırak, diğerlerinin işaretini kaldır."
        ),
    )


def _categories(
    ties: Sequence[TiedShot], clips: Sequence[_Clip], excluded: Iterable[str]
) -> list[dict[str, Any]]:
    skipped = set(excluded)
    candidates = [c.media_id for c in clips if c.media_id not in skipped][:MAX_QUESTION_CLIPS]
    return [
        {
            "key": intent_key(tie.intent),
            "label": category_label(tie.intent),
            "op": tie.intent.op,
            "intent_id": tie.intent.intent_id,
            "creator_text": tie.intent.creator_text,
            "candidate_media_ids": candidates,
            "suggested_media_ids": list(tie.media_ids),
        }
        for tie in ties
    ]


def _asked(question: dict[str, Any], ties: Sequence[TiedShot]) -> list[tuple[str, int]]:
    count = {intent_key(tie.intent): len(tie.media_ids) for tie in ties}
    return [(c["label"], count[c["key"]]) for c in question["categories"] if c["key"] in count]


def tie_question(
    ties: Sequence[TiedShot], clips: Sequence[_Clip], *, excluded: Iterable[str] = ()
) -> tuple[str, dict[str, Any]] | None:
    """(the question text, the ``clip_question`` payload), or None when nothing can be tapped.

    One picker category per tied shot: every clip the creator did not leave out is offered,
    the tied ones pre-ticked. The tapped clips become the shot's clips.
    """
    if not ties:
        return None
    question = build_clip_question(
        _categories(ties, clips, excluded), media_kinds={c.media_id: c.kind for c in clips}
    )
    if question is None:
        return None
    return _question_text(_asked(question, ties)), question


def with_tie_categories(
    text: str | None,
    clip_question: dict[str, Any],
    ties: Sequence[TiedShot],
    clips: Sequence[_Clip],
    *,
    excluded: Iterable[str] = (),
) -> tuple[str | None, dict[str, Any]]:
    """The card this turn already asks, with the tied shots added: one card, one turn.

    The card keeps its id (the creator's taps bind to it) and its own categories first; a tie
    the card has no room for is asked on the next turn.
    """
    if not ties:
        return text, clip_question
    merged = build_clip_question(
        [*clip_question["categories"], *_categories(ties, clips, excluded)],
        question_id=clip_question.get("question_id"),
        media_kinds={c.media_id: c.kind for c in clips},
    )
    if merged is None:
        return text, clip_question
    existing = {c["key"] for c in clip_question["categories"]}
    added = _asked(
        {"categories": [c for c in merged["categories"] if c["key"] not in existing]}, ties
    )
    if not added:
        return text, clip_question
    return " ".join(p for p in (text, _question_text(added)) if p), merged
