"""An exclusion that clashes with another ask, or that look-alike clips share, asks (KRI-761).

Prod thread A9100235 (Merope café M1): "skip the clip where Caro bends and puts the plate
down" matched TWO clips at the resolver's bar, the one the creator had just pinned first and
the one to drop. The pin kept its clip (``clip_scope.excluded_clips``), but the creator was
never told, the file-name twin "M1_05 klibini" matched nothing, and the next draft's
resolver matched neither, so the dropped clip played.

``clip_scope`` leaves an exclusion unapplied when a clip another live ask pins is among its
matches; this module says so and asks, with the same clip picker the other asks use, instead
of guessing:

* ``contested_exclusions``: the resolver-matched excludes that name a pinned clip (a first
  or last placement, a label the creator wrote, media the strategy places) or whose 2+
  matches are only just over the bar (look-alikes it could not tell apart). What the
  creator tapped or typed is never contested, so an answer is never asked twice.
* ``exclusion_question``: the picker for them, with the pinned clips left out of the
  candidates and the matches pre-ticked.
* ``carry_forward_exclusions``: an ask the resolver matched in the draft before and matches
  nothing in this turn keeps that earlier match (the resolver is not deterministic, and the
  whole request is re-planned every turn).
* ``exclusions_to_ask`` (KRI-807): the contested excludes plus every exclude that left no
  clip out ("skip the blurry shot of the empty table" matched nothing on Don Julio M1).
  Those used to end in a free-text "Tell me which ones"; they ask with the same picker,
  the resolver's below-bar or vision-check suspects pre-ticked, never left out unasked.

Nothing here reads creator words: the matches are the resolver's, the answers the creator's.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from app.agents._schemas.creator_agent import CreativeStrategy
from app.kria.clip_scope import placed_media
from app.kria.reply_language import say
from app.schemas.clip_intents import (
    CREATOR_EVIDENCE,
    ResolvedClipIntent,
)
from app.services.clip_selection import (
    MAX_QUESTION_CLIPS,
    build_clip_question,
    category_label,
    intent_key,
)

# Two clips only this close to the exclude bar are look-alikes the resolver could not tell
# apart ("skip the two very dark clips" matches both well above it).
BORDERLINE_BELOW = 0.9
# The same ask in the next turn's words shares at least this share of its key words.
_SAME_ASK_OVERLAP = 0.6
EARLIER_DRAFT_EVIDENCE = "earlier draft"


class _Clip(Protocol):
    media_id: str
    kind: str


@dataclass(frozen=True)
class ContestedExclusion:
    intent: ResolvedClipIntent
    # Every clip the resolver matched for the ask, in its order.
    matched: tuple[str, ...]
    # Every clip another live ask pins or the strategy places (not only the matched ones).
    protected: frozenset[str]
    # KRI-807: an exclude that matched nothing: the clips the resolver only suspected.
    suspected: tuple[str, ...] = ()

    @property
    def blocked(self) -> tuple[str, ...]:
        """The matched clips that are pinned: they stay whatever the creator answers."""
        return tuple(media_id for media_id in self.matched if media_id in self.protected)

    @property
    def suggested(self) -> list[str]:
        """The clips the picker pre-ticks: the matches, else the suspects, never a pin."""
        return [m for m in self.matched or self.suspected if m not in self.protected]


def pinned_clips(
    intents: Iterable[ResolvedClipIntent], strategy: CreativeStrategy
) -> frozenset[str]:
    """Clips the creator pinned with a first/last ask or a label of their own, plus media the
    strategy places or plays (a reaction beat, closing media, a montage audio source).

    A group, an include, a capture-time order or a model-written clip description assigns
    many clips and says nothing about any one of them, so none of those pins.
    """
    pinned = set(placed_media(strategy))
    for intent in intents:
        if intent.status != "resolved":
            continue
        pins_clips = (
            intent.op == "order" and intent.position is not None and intent.order_by is None
        ) or (intent.op == "label" and bool(intent.creator_text) and not intent.placeholder)
        if pins_clips:
            pinned.update(a.media_id for a in intent.assignments)
    return frozenset(pinned)


def _contested(intent: ResolvedClipIntent, protected: frozenset[str]) -> ContestedExclusion | None:
    if intent.op != "exclude" or intent.status != "resolved" or not intent.assignments:
        return None
    if any(a.evidence in CREATOR_EVIDENCE for a in intent.assignments):
        return None  # a tap or a typed file name is the creator's own word
    matched = tuple(dict.fromkeys(a.media_id for a in intent.assignments))
    names_a_pin = any(media_id in protected for media_id in matched)
    look_alikes = len(matched) >= 2 and (
        min(a.confidence for a in intent.assignments) < BORDERLINE_BELOW
    )
    return ContestedExclusion(intent, matched, protected) if names_a_pin or look_alikes else None


def contested_exclusions(
    intents: Sequence[ResolvedClipIntent], strategy: CreativeStrategy
) -> list[ContestedExclusion]:
    """The resolver-matched excludes the creator should settle, in the order written."""
    protected = pinned_clips(intents, strategy)
    return [c for intent in intents if (c := _contested(intent, protected)) is not None]


def exclusions_to_ask(
    intents: Sequence[ResolvedClipIntent],
    strategy: CreativeStrategy,
    *,
    suspects: Mapping[str, Sequence[str]],
    clip_count: int,
) -> list[ContestedExclusion]:
    """Every exclude the creator should settle, in the order written (KRI-807).

    The contested ones, plus each exclude that leaves no clip out: the resolver matched
    nothing confidently, so the skip would silently not happen. ``suspects`` (intent id ->
    clips) are what the resolver put forward below the bar or for a vision check; the
    picker pre-ticks them and only the creator's tap leaves them out. A project with one
    clip is never asked which of its clips to leave out (that would leave nothing).
    """
    protected = pinned_clips(intents, strategy)
    asked: list[ContestedExclusion] = []
    for intent in intents:
        contested = _contested(intent, protected)
        if contested is not None:
            asked.append(contested)
        elif (
            intent.op == "exclude"
            and intent.status == "resolved"
            and not intent.assignments
            and clip_count >= 2
        ):
            suspected = tuple(dict.fromkeys(suspects.get(intent.intent_id, ())))
            asked.append(ContestedExclusion(intent, (), protected, suspected))
    return asked


def _question_text(labels: list[str]) -> str:
    if len(labels) == 1:
        return say(
            en=(
                f'I couldn\'t tell which of your clips to leave out for "{labels[0]}". '
                "Tap the ones to leave out, or tell me none."
            ),
            tr=(
                f'"{labels[0]}" için hangi klipleri çıkaracağımı anlayamadım. '
                "Çıkarılacak olanlara dokun ya da hiçbirini çıkarmamamı söyle."
            ),
        )
    shown = ", ".join(f'"{label}"' for label in labels[:4])
    more = say(
        en=f" and {len(labels) - 4} more" if len(labels) > 4 else "",
        tr=f" ve {len(labels) - 4} tane daha" if len(labels) > 4 else "",
    )
    return say(
        en=(
            f"I couldn't tell which of your clips to leave out for {shown}{more}. "
            "Tap the clips to leave out for each one, or mark the ones with none."
        ),
        tr=(
            f"Şunlar için hangi klipleri çıkaracağımı anlayamadım: {shown}{more}. "
            "Her biri için çıkarılacak klipleri seç ya da hiçbirini çıkarmayacaklarını işaretle."
        ),
    )


def exclusion_question(
    contested: Sequence[ContestedExclusion], clips: Sequence[_Clip]
) -> tuple[str, dict[str, Any]] | None:
    """(the question text, the ``clip_question`` payload), or None when nothing can be tapped.

    One picker category per contested exclude. The creator taps the clips to LEAVE OUT; a
    pinned clip is never offered, and the matches that are not pinned come pre-ticked. The
    answer rides the same ``clip_selection`` store as every other category, keyed by op
    "exclude", so it holds on later turns.
    """
    if not contested:
        return None
    protected = contested[0].protected
    candidates = [c.media_id for c in clips if c.media_id not in protected][:MAX_QUESTION_CLIPS]
    categories = [
        {
            "key": intent_key(asked.intent),
            "label": category_label(asked.intent),
            "op": "exclude",
            "intent_id": asked.intent.intent_id,
            "creator_text": None,
            "candidate_media_ids": candidates,
            "suggested_media_ids": asked.suggested,
        }
        for asked in contested
    ]
    question = build_clip_question(categories, media_kinds={c.media_id: c.kind for c in clips})
    if question is None:
        return None
    return _question_text([c["label"] for c in question["categories"]]), question


def with_exclusion_categories(
    text: str | None,
    clip_question: dict[str, Any],
    asked: Sequence[ContestedExclusion],
    clips: Sequence[_Clip],
) -> tuple[str | None, dict[str, Any]]:
    """The resolver's clip picker with the excludes to ask added: one card, one turn.

    KRI-807: on Don Julio M1 the resolver asked which clip shows the red-cap friend, and the
    turn ended there; the skip would only have been asked a turn later. The picker keeps its
    id (the creator's taps bind to it) and its own categories first; an exclude row the card
    has no room for is asked on the next turn, as before.
    """
    extra = exclusion_question(asked, clips)
    if extra is None:
        return text, clip_question
    merged = build_clip_question(
        [*clip_question["categories"], *extra[1]["categories"]],
        question_id=clip_question.get("question_id"),
        media_kinds={c.media_id: c.kind for c in clips},
    )
    if merged is None:
        return text, clip_question
    kept = {c["key"] for c in merged["categories"]}
    added = [c for c in extra[1]["categories"] if c["key"] in kept]
    if not added:
        return text, clip_question
    return " ".join(p for p in (text, _question_text([c["label"] for c in added])) if p), merged


def _key_words(intent: ResolvedClipIntent) -> set[str]:
    return {word for word in intent_key(intent).partition(":")[2].split("-") if word}


def _same_ask(now: ResolvedClipIntent, earlier: ResolvedClipIntent) -> bool:
    if now.intent_id == earlier.intent_id:
        return True
    words, other = _key_words(now), _key_words(earlier)
    return bool(words and other) and len(words & other) / len(words | other) >= _SAME_ASK_OVERLAP


def carry_forward_exclusions(
    intents: Sequence[ResolvedClipIntent],
    earlier: Sequence[ResolvedClipIntent],
    *,
    clip_ids: set[str],
) -> tuple[list[ResolvedClipIntent], int]:
    """``intents`` with each empty exclude given the earlier draft's match for the same ask.

    The planner re-reads the whole request every turn and the resolver is not deterministic:
    an ask that matched a clip in the draft before can match nothing now. Only an exclude
    that matched NOTHING this turn takes the earlier clips, only for the same ask (its id,
    or mostly the same words), and only clips still attached. Returns ``(intents, carried)``.
    """
    prior = [e for e in earlier if e.op == "exclude" and e.status == "resolved" and e.assignments]
    if not prior:
        return list(intents), 0
    out: list[ResolvedClipIntent] = []
    carried = 0
    for intent in intents:
        if intent.op != "exclude" or intent.status != "resolved" or intent.assignments:
            out.append(intent)
            continue
        source = next((e for e in prior if _same_ask(intent, e)), None)
        assignments = [
            a
            if a.evidence in CREATOR_EVIDENCE
            else a.model_copy(update={"evidence": EARLIER_DRAFT_EVIDENCE})
            for a in (source.assignments if source else [])
            if a.media_id in clip_ids
        ]
        if not assignments:
            out.append(intent)
            continue
        out.append(intent.model_copy(update={"assignments": assignments}))
        carried += 1
    return out, carried


def earlier_exclusions(snapshot: object) -> list[ResolvedClipIntent]:
    """The ``exclude`` intents a draft snapshot carries; anything malformed reads as none."""
    strategy = snapshot.get("strategy") if isinstance(snapshot, dict) else None
    rows = strategy.get("resolved_clip_intents") if isinstance(strategy, dict) else None
    if not isinstance(rows, list):
        return []
    out: list[ResolvedClipIntent] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("op") != "exclude":
            continue
        try:
            out.append(ResolvedClipIntent.model_validate(row))
        except ValueError:  # a pydantic ValidationError: skip the row, keep the turn
            continue
    return out


__all__ = [
    "BORDERLINE_BELOW",
    "EARLIER_DRAFT_EVIDENCE",
    "ContestedExclusion",
    "carry_forward_exclusions",
    "contested_exclusions",
    "earlier_exclusions",
    "exclusion_question",
    "exclusions_to_ask",
    "pinned_clips",
    "with_exclusion_categories",
]
