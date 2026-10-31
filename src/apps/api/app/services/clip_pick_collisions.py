"""One photo on two ordered beats is the creator's to settle (KRI-1125).

Leverkusen M4 run 1: the creator tapped the team-bus photo for "the warm-up" (a tile-numbering
slip), then corrected it in plain words ("the warm-up is the photo of the players on the pitch,
not the team bus"). The thread's picker answers are folded every turn and a typed message never
replaces one, so the warm-up stayed on the bus ("creator selected", settled BEFORE the
resolver); the bus photo sat on two ordered beats, nothing downstream said so (the second row is
``contained``), and the real warm-up photo played 9th.

* ``find_pick_collisions``: two positionless ``order`` beats resolved onto the same single
  IMAGE, where at least one was settled by a tap. Never a video (one take can hold two moments,
  KRI-779/796) and never a multi-clip group that merely contains the photo.
* ``with_collision_question``: the creator is asked, with the same clip picker the other asks
  use, about the PICKED beat (the older tap when both are taps, so the newer answer stands),
  the contested photo left out of the candidates. The new tap is the newest answer, so it wins
  (``fold_clip_selections``).
* ``plan_without_pick_collisions``: only inside such a collision, a typed message newer than the
  tap that names the beat drops the tap and sends the beat back to the resolver, which reads the
  correction. If it finds nothing the picker asks again (its own near-misses pre-ticked, never
  the stale tap). Outside a collision no word is read: the tap's own echo carries the beat's
  words, and voiding taps on any re-mention brings back the re-asked-forever bugs (KRI-947/950).

Nothing here reads a model's answer for meaning: the matches are the resolver's, the taps the
creator's.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

import structlog

from app.kria.reply_language import say
from app.schemas.clip_intents import CREATOR_SELECTED_EVIDENCE, ClipIntent, ResolvedClipIntent
from app.services.choice_questions import CONTENT_KEY, EVENT_TYPE_KEY
from app.services.clip_selection import (
    MAX_QUESTION_CATEGORIES,
    MAX_QUESTION_CLIPS,
    ClipSelections,
    SelectionEntry,
    build_clip_question,
    category_label,
    intent_key,
)
from app.text_fold import loose_words

if TYPE_CHECKING:
    from app.services.clip_intent_planning import PlannedIntentResolution
    from app.services.clip_intent_resolution import IntentClip

log = structlog.get_logger()

# Words that do not say WHICH beat a line is about ("the warm-up photo" is the "warm up" beat).
_FILLER = frozenset(
    {
        # English
        "the", "a", "an", "my", "our", "your", "his", "her", "their", "its", "of", "and",
        "with", "in", "on", "at", "to", "for", "photo", "picture", "clip", "video", "shot",
        # Turkish (folded: no diacritics)
        "bir", "bu", "su", "o", "ve", "ile", "icin", "fotograf", "klip", "video",
    }
)  # fmt: skip


@dataclass(frozen=True)
class PickCollision:
    """``photo`` is on ``picked`` and on every beat of ``others``; ``picked`` is the one asked."""

    photo: str
    picked: ResolvedClipIntent
    others: tuple[ResolvedClipIntent, ...]


def _lone_photo(intent: ResolvedClipIntent, kinds: Mapping[str, str]) -> str | None:
    """The one IMAGE a positionless ordered beat stands on, else None."""
    if (
        intent.op != "order"
        or intent.status != "resolved"
        or intent.position is not None
        or intent.order_by is not None
        or intent.ordered_members is not None
    ):
        return None
    media = {a.media_id for a in intent.assignments}
    if len(media) != 1:
        return None
    (media_id,) = media
    return media_id if kinds.get(media_id) == "image" else None


def _tapped(intent: ResolvedClipIntent) -> bool:
    return any(a.evidence == CREATOR_SELECTED_EVIDENCE for a in intent.assignments)


def find_pick_collisions(
    intents: Sequence[ResolvedClipIntent],
    clips: Sequence[IntentClip],
    selections: ClipSelections | None,
    *,
    reopened: Callable[[ResolvedClipIntent], bool] = lambda _intent: False,
) -> list[PickCollision]:
    """Photos that stand for two ordered beats where the creator's tap decided at least one.

    ``reopened`` marks a beat whose tap a typed correction dropped and the resolver re-read
    (``plan_without_pick_collisions``): if the resolver still lands it on a photo another beat
    holds, it is asked like a tap would be. Which beat is asked: a reopened one; else the
    older tap (the newer answer stands), the later beat when both were tapped at once. Two
    beats only the resolver matched are left to the sequence rule, as before.
    """
    kinds = {clip.media_id: clip.kind for clip in clips}
    holders: dict[str, list[ResolvedClipIntent]] = {}
    for intent in intents:
        photo = _lone_photo(intent, kinds)
        if photo is not None:
            holders.setdefault(photo, []).append(intent)
    place = {id(intent): index for index, intent in enumerate(intents)}

    def tap_seq(intent: ResolvedClipIntent) -> int:
        entry = selections.match(intent) if selections else None
        return entry.seq if entry is not None else 0

    out: list[PickCollision] = []
    for photo, beats in holders.items():
        if len(beats) < 2:
            continue
        again = [b for b in beats if reopened(b)]
        taps = [b for b in beats if _tapped(b)]
        if again:
            picked = again[-1]
        elif taps:
            picked = min(taps, key=lambda b: (tap_seq(b), -place[id(b)]))
        else:
            continue
        out.append(PickCollision(photo, picked, tuple(b for b in beats if b is not picked)))
    return out[:MAX_QUESTION_CATEGORIES]


def names_the_beat(text: str, intent: ClipIntent) -> bool:
    """True when ``text`` says the beat's words ("the warm-up" in "the warm-up photo is ...")."""
    words = [w for w in loose_words(intent.attribute) if w not in _FILLER]
    said = set(loose_words(text))
    return bool(words) and all(word in said for word in words)


def typed_after_pick(
    events: Sequence[tuple[str, Any]], entry: SelectionEntry, *, answer_keys: Collection[str]
) -> list[str]:
    """The creator's typed lines newer than the tap that settled ``entry``.

    A tap, a card answer or anything the server wrote is not typed; neither is the tap's own
    echo (it carries the beat's words, and is the tap itself).
    """
    tap = None
    for index, (role, payload) in enumerate(events):
        selection = payload.get("clip_selection") if role == "user" and payload else None
        if isinstance(selection, Mapping) and selection.get("question_id") == entry.question_id:
            tap = index
    if tap is None:
        return []
    typed: list[str] = []
    for role, payload in events[tap + 1 :]:
        if role != "user" or not isinstance(payload, Mapping):
            continue
        text = payload.get(CONTENT_KEY)
        if (
            payload.get(EVENT_TYPE_KEY) == "user_message"
            and isinstance(text, str)
            and text
            and not set(answer_keys) & payload.keys()
        ):
            typed.append(text)
    return typed


def _corrected(
    collision: PickCollision,
    selections: ClipSelections,
    events: Sequence[tuple[str, Any]],
    answer_keys: Collection[str],
) -> bool:
    """A typed line newer than the picked beat's tap names that beat."""
    entry = selections.match(collision.picked)
    return entry is not None and any(
        names_the_beat(text, collision.picked)
        for text in typed_after_pick(events, entry, answer_keys=answer_keys)
    )


def _creator_words(planned: PlannedIntentResolution) -> dict[str, str]:
    """intent id -> the creator's own words for it (the planner's quotes kept on the inventory)."""
    rows = ((planned.inventory or {}).get("output") or {}).get("intents") or []
    return {
        row["intent_id"]: row["source_quote"]
        for row in rows
        if isinstance(row, dict) and row.get("intent_id") and row.get("source_quote")
    }


def _question_text(labels: list[tuple[str, str]]) -> str:
    """The plain-words reason: ``(asked beat, the beat it clashes with)`` per collision."""
    if len(labels) == 1:
        asked, other = labels[0]
        return say(
            en=(
                f'The same photo is down for both "{asked}" and "{other}". '
                f'Which clip is "{asked}"? Tap it, or tell me there isn\'t one.'
            ),
            tr=(
                f'"{asked}" ve "{other}" için aynı fotoğraf seçilmiş. '
                f'"{asked}" hangi klip? Ona dokun ya da olmadığını söyle.'
            ),
        )
    shown = ", ".join(f'"{asked}"' for asked, _other in labels[:4])
    more = say(
        en=f" and {len(labels) - 4} more" if len(labels) > 4 else "",
        tr=f" ve {len(labels) - 4} tane daha" if len(labels) > 4 else "",
    )
    return say(
        en=(
            f"The same photo is down for more than one of your moments. Tap the clip for "
            f"each of {shown}{more}, or mark the ones that have none."
        ),
        tr=(
            f"Birden fazla anın için aynı fotoğraf seçilmiş. Şunlar için klibi seç: "
            f"{shown}{more}. Klibi olmayanları işaretle."
        ),
    )


def with_collision_question(
    planned: PlannedIntentResolution,
    collisions: Sequence[PickCollision],
    clips: Sequence[IntentClip],
) -> PlannedIntentResolution:
    """``planned`` with the clip picker asking about each collision's picked beat.

    The picker lists every clip the beat accepts except the contested photo; no resolver ever
    read a tapped beat, so nothing is pre-ticked here. A picker the resolver already built
    keeps its id and its rows first (the creator's taps bind to it); a degraded resolution
    (a provider stop, a pending check) is left to say so.
    """
    resolution = planned.resolution
    if not collisions or resolution.status not in {"resolved", "needs_creator"}:
        return planned
    kinds = {clip.media_id: clip.kind for clip in clips}
    words = _creator_words(planned)
    categories, labels = [], []
    for collision in collisions:
        intent = collision.picked
        label = category_label(intent, creator_words=words.get(intent.intent_id))
        labels.append(
            (
                label,
                category_label(
                    collision.others[0], creator_words=words.get(collision.others[0].intent_id)
                ),
            )
        )
        categories.append(
            {
                "key": intent_key(intent),
                "label": label,
                "op": intent.op,
                "intent_id": intent.intent_id,
                "creator_text": intent.creator_text,
                "candidate_media_ids": [
                    m
                    for m, kind in kinds.items()
                    if m != collision.photo and intent.accepts_source_kind(kind)
                ][:MAX_QUESTION_CLIPS],
                "suggested_media_ids": [],
            }
        )
    asked = build_clip_question(categories, media_kinds=kinds)
    if asked is None:
        return planned
    existing = resolution.clip_question
    clip_question = asked
    if existing is not None:
        clip_question = build_clip_question(
            [*existing["categories"], *asked["categories"]],
            question_id=existing.get("question_id"),
            media_kinds=kinds,
        )
        kept = {c["key"] for c in (clip_question or {}).get("categories", [])}
        if clip_question is None or not all(c["key"] in kept for c in asked["categories"]):
            return planned  # no room on the card; the next turn asks again
    text = " ".join(p for p in (resolution.question, _question_text(labels)) if p)
    reopened = {c.picked.intent_id for c in collisions}
    log.info(
        "kria_pick_collision_asked",
        collisions=len(collisions),
        merged=existing is not None,
        both_tapped=sum(1 for c in collisions if any(_tapped(b) for b in c.others)),
    )
    return replace(
        planned,
        resolution=replace(
            resolution,
            intents=[
                i.model_copy(
                    update={"status": "needs_creator", "assignments": [], "question": text}
                )
                if i.intent_id in reopened
                else i
                for i in resolution.intents
            ],
            status="needs_creator",
            question=text,
            clip_question=clip_question,
            diagnostics={
                **(resolution.diagnostics or {}),
                # A resolution that was whole says why it now asks; one already asking keeps
                # its own stage and reason.
                **(
                    {"stage": "pick_collision", "reason": "same_photo_two_beats"}
                    if resolution.question is None
                    else {}
                ),
                "pick_collisions": len(collisions),
            },
        ),
    )


async def plan_without_pick_collisions(
    plan: Callable[..., Awaitable[PlannedIntentResolution]],
    *,
    thread_events: Sequence[tuple[str, Any]],
    answer_keys: Collection[str],
    **plan_kwargs: Any,
) -> PlannedIntentResolution:
    """``plan`` (``plan_and_resolve_clip_intents``) with no photo silently on two ordered beats.

    A collision whose tap a newer typed line corrects is re-planned once with that tap dropped
    (the resolver then reads the correction); every collision that remains is asked.
    ``answer_keys`` are the payload keys of a tap or card answer, which are not typed lines.
    """
    planned = await plan(**plan_kwargs)
    selections: ClipSelections | None = plan_kwargs.get("clip_selections")
    if not selections:
        return planned
    clips = plan_kwargs["clips"]
    collisions = find_pick_collisions(planned.resolution.intents, clips, selections)
    if not collisions:
        return planned
    corrected = [c for c in collisions if _corrected(c, selections, thread_events, answer_keys)]
    if corrected:
        kept = selections
        for collision in corrected:
            kept = kept.without(collision.picked)
        dropped = {c.picked.intent_id for c in corrected}
        dropped_keys = {intent_key(c.picked) for c in corrected}
        log.info(
            "kria_pick_collision_reopened", reopened=len(corrected), kept_taps=len(kept.by_key)
        )
        planned = await plan(**{**plan_kwargs, "clip_selections": kept or None})
        collisions = find_pick_collisions(
            planned.resolution.intents,
            clips,
            kept,
            reopened=lambda i: i.intent_id in dropped or intent_key(i) in dropped_keys,
        )
    return with_collision_question(planned, collisions, clips)
