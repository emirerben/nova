"""Late text gates drop or shrink the one bad item instead of failing the video (KRI-1376).

The creation text pass runs after the creator approved. Each of its gates used to raise and
fail the whole render (`creation_text_failed`, 6 of 13 prod failures in 14 days). KRI-129:
the approved draft renders and says what Kria could not do. A gate is now repair -> degrade ->
fail:

* a gate whose typed reason names a composer op the replay rejected (``_STRATEGY``) drops that
  one op and keeps every other op, never the whole lane (KRI-767);
* a composer that reports part of the ask as unmet, a missed text hierarchy and an editorial
  beat with no explanation render without that part (``action="shrunk"``);
* everything else is re-raised unchanged, so ``_classify_phone_plan_failure`` maps it as before.

The copy, layer and replay checks still run on every candidate program, so a drop can never
take the creator's confirmed words off screen; an op carrying a brief literal is never dropped.
Every drop is a ``TextDrop``: the note the creator reads and the admin trace event.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Generic, Literal, TypeVar

from app.kria.reply_language import say

if TYPE_CHECKING:
    from app.kria.brief import BriefRequirement
    from app.services.creation_text_composition import CreationTextCompositionError

T = TypeVar("T")

TextGateReason = Literal[
    "copy_changed",
    "unasked_text",
    "replay_failed",
    "research_missing",
    "layer_shape",
    "hierarchy_unmet",
    "editorial_unmet",
    "incomplete",
]

# How a rejected program is narrowed, per typed reason. A reason not listed is not degraded.
#   one_op       drop the single composer op whose absence lets the whole program pass;
#   added_text   drop the composer's added lines no ask authorized (they are all unasked).
_STRATEGY: Mapping[str, Literal["one_op", "added_text"]] = {
    "copy_changed": "one_op",
    "replay_failed": "one_op",
    "research_missing": "one_op",
    "layer_shape": "one_op",
    "unasked_text": "added_text",
}

# Rejections whose item renders in part: the words are on screen, one asked behavior is not.
_SHRINKABLE = frozenset({"incomplete", "hierarchy_unmet", "editorial_unmet"})

# Like `phone_plan_repair._MAX_REPAIR_PASSES`: a bounded number of narrowing rounds.
MAX_DEGRADE_PASSES = 3


@dataclass(frozen=True)
class TextDrop:
    """One item the text pass left out or did only in part, and why."""

    reason: TextGateReason
    action: Literal["dropped", "shrunk"]
    # The creator-visible words the drop concerns (quoted in the note), when there are some.
    text: str | None = None
    # The dropped editor op's name, for the admin trace only.
    op: str | None = None

    def note(self) -> str | None:
        """The "What Kria changed" line, in the bound reply language; None = nothing to say.

        A line no ask authorized was never the creator's, so dropping it is silent, as
        ``_drop_unasked_added_text`` already is.
        """
        from app.pipeline.phone_plan_repair import _quoted  # noqa: PLC0415

        quoted = _quoted(self.text) if self.text and self.text.strip() else None
        if self.reason == "unasked_text":
            return None
        if self.reason == "incomplete":
            return say(
                en="I couldn't do every part of your text request, so the video shows the "
                "parts I could",
                tr="Yazı isteğinin her kısmını yapamadım, videoda yapabildiğim kısımlar var",
            )
        if self.reason == "hierarchy_unmet":
            return say(
                en="I couldn't size your texts exactly as you asked",
                tr="Yazıları tam istediğin boyutlarda ayarlayamadım",
            )
        if self.reason == "editorial_unmet":
            return say(
                en="Left out one explanation that didn't fit its part of the video",
                tr="Videonun ilgili kısmına sığmayan bir açıklamayı eklemedim",
            )
        if quoted is None:
            return say(
                en="Left out one text change I couldn't make fit",
                tr="Sığdıramadığım bir yazı değişikliğini eklemedim",
            )
        if self.reason == "copy_changed":
            return say(
                en="Kept {text} exactly as you wrote it and left out the change I couldn't "
                "make to it",
                tr="{text} metnini yazdığın gibi bıraktım, ona yapamadığım değişikliği eklemedim",
                text=quoted,
            )
        return say(
            en="Left out one text change for {text} because I couldn't make it fit",
            tr="{text} için sığdıramadığım bir yazı değişikliğini eklemedim",
            text=quoted,
        )

    def event(self) -> dict[str, Any]:
        """The ``requirement_recovery`` trace payload (admin only; never exception text)."""
        return {
            "decision": "render_with_notes",
            "lane": "text",
            "reason": self.reason,
            "action": self.action,
            **({"item": self.text[:120]} if self.text else {}),
            **({"op": self.op} if self.op else {}),
        }


def text_drop_notes(drops: Sequence[TextDrop]) -> list[str]:
    """Each drop's note once, in order, in the caller's bound reply language."""
    notes: list[str] = []
    for drop in drops:
        note = drop.note()
        if note and note not in notes:
            notes.append(note[:160])
    return notes


@dataclass(frozen=True)
class Degraded(Generic[T]):
    """A program that passed every gate, the composer ops it kept, and what was dropped.

    ``cause`` is the first rejection, kept so a caller that finds nothing asked left on screen
    re-raises exactly what the gate raised.
    """

    result: T
    ops: list[dict]
    drops: list[TextDrop]
    cause: CreationTextCompositionError | None


def shrink(error: CreationTextCompositionError) -> TextDrop:
    """Render without the one asked behavior ``error`` names, or re-raise it when it is not one
    of the gates that may (``_SHRINKABLE``)."""
    if error.reason not in _SHRINKABLE:
        raise error
    return TextDrop(reason=error.reason, action="shrunk")  # type: ignore[arg-type]


def literal_ops(
    requirements: Sequence[BriefRequirement] | None,
) -> Callable[[dict], bool]:
    """Whether a composer op places the creator's own words (a live brief literal): never dropped.

    A line holding confirmed copy is already kept by the copy check every candidate replays; a
    literal the brief asked for has no such row before the composer adds it.
    """
    from app.text_fold import fold_text  # noqa: PLC0415

    literals = {
        fold_text(req.literal) for req in requirements or () if req.live and req.literal
    } - {""}

    def protected(op: dict) -> bool:
        if isinstance(op.get("research"), dict):
            return True
        return op.get("op") == "add_text" and fold_text(str(op.get("text") or "")) in literals

    return protected


def op_text(op: Mapping[str, Any], lane: Sequence[Mapping[str, Any]]) -> str | None:
    """The words an op adds or the first bar it addresses, for the note; None when unclear."""
    if op.get("op") == "add_text":
        return str(op.get("text") or "") or None
    ids = op.get("target_ids") or (op.get("selector") or {}).get("ids")
    if isinstance(ids, list) and ids:
        row = next((row for row in lane if row.get("id") == ids[0]), None)
        if row is not None:
            return str(row.get("text") or "") or None
    index = op.get("bar_index")
    if index is None and isinstance(op.get("bar_indexes"), list) and op["bar_indexes"]:
        index = op["bar_indexes"][0]
    if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(lane):
        return str(lane[index].get("text") or "") or None
    return None


def degrade_text_ops(
    ops: list[dict],
    attempt: Callable[[list[dict]], T],
    *,
    lane: Sequence[Mapping[str, Any]],
    protected: Callable[[dict], bool],
) -> Degraded[T]:
    """``attempt(ops)``, narrowing ``ops`` one rejected item at a time until it passes.

    ``ops`` are the composer's own ops, the only ones a degrade may drop (approved research,
    seeded words and server-timed sequences are added inside ``attempt``). Each round tries
    every single drop (or the unasked lines, together) and keeps the first that passes. When no
    drop passes but one turns the rejection into a different degradable one, that drop is kept
    and the next round narrows the new rejection. A timeout, an untyped rejection, or nothing
    left to try after ``MAX_DEGRADE_PASSES`` rounds re-raises the first rejection unchanged.
    """
    from app.services.creation_text_composition import (  # noqa: PLC0415 - imports this module
        CreationTextCompositionError,
        CreationTextTimeoutError,
    )

    try:
        return Degraded(attempt(ops), list(ops), [], None)
    except CreationTextTimeoutError:
        raise
    except CreationTextCompositionError as exc:
        first = exc
    error = first
    kept = list(ops)
    drops: list[TextDrop] = []
    for _ in range(MAX_DEGRADE_PASSES):
        strategy = _STRATEGY.get(error.reason or "")
        if strategy is None:
            raise first
        progress: tuple[frozenset[int], CreationTextCompositionError] | None = None
        for dropped in _candidates(strategy, kept, protected, named=error.op_index):
            trial = [op for index, op in enumerate(kept) if index not in dropped]
            try:
                result = attempt(trial)
            except CreationTextTimeoutError:
                raise
            except CreationTextCompositionError as exc:
                if progress is None and _key(exc) != _key(error) and exc.reason in _STRATEGY:
                    progress = (dropped, exc)
                continue
            drops.extend(_dropped(error.reason, kept, dropped, lane))
            stand_ins = _stand_ins(kept, dropped, lane, protected)
            if stand_ins:
                # The composer moved the confirmed text and reworded it on the way: the reworded
                # line goes too, so no invented words stay next to the creator's.
                wider = [op for index, op in enumerate(kept) if index not in dropped | stand_ins]
                try:
                    result, trial = attempt(wider), wider
                    drops.extend(_dropped("unasked_text", kept, stand_ins, lane))
                except CreationTextCompositionError:
                    pass
            return Degraded(result, trial, drops, first)
        if progress is None:
            raise first
        dropped, next_error = progress
        drops.extend(_dropped(error.reason, kept, dropped, lane))
        kept = [op for index, op in enumerate(kept) if index not in dropped]
        error = next_error
    raise first


def _key(exc: CreationTextCompositionError) -> tuple[str | None, str | None, str]:
    return exc.reason, exc.item_id, str(exc)


def _candidates(
    strategy: str, ops: list[dict], protected: Callable[[dict], bool], *, named: int | None
) -> list[frozenset[int]]:
    """The drops to try, best first: the op the rejection named, then the latest op first.

    A later op is the one that can read what an earlier one left (a style after a split), so it
    goes before the op it depends on.
    """
    droppable = [index for index, op in enumerate(ops) if not protected(op)]
    if strategy == "added_text":
        added = frozenset(index for index in droppable if ops[index].get("op") == "add_text")
        return [added] if added else []
    order = sorted(droppable, key=lambda index: (index != named, -index))
    return [frozenset({index}) for index in order]


_REMOVALS = frozenset({"remove_text", "remove_texts"})


def _stand_ins(
    ops: list[dict],
    dropped: frozenset[int],
    lane: Sequence[Mapping[str, Any]],
    protected: Callable[[dict], bool],
) -> frozenset[int]:
    """The added lines meant to replace the confirmed text a dropped removal took off screen:
    they repeat at least half of its words."""
    from app.text_fold import fold_text  # noqa: PLC0415

    removed = [
        set(fold_text(text).split())
        for index in dropped
        if ops[index].get("op") in _REMOVALS and (text := op_text(ops[index], lane))
    ]
    if not removed:
        return frozenset()
    added = {
        index
        for index, op in enumerate(ops)
        if index not in dropped
        and op.get("op") == "add_text"
        and not protected(op)
        and any(
            len(words & set(fold_text(str(op.get("text") or "")).split())) * 2 >= len(words)
            for words in removed
        )
    }
    return frozenset(added)


def _dropped(
    reason: str | None,
    ops: list[dict],
    dropped: frozenset[int],
    lane: Sequence[Mapping[str, Any]],
) -> list[TextDrop]:
    reason = reason if reason in _STRATEGY else "replay_failed"
    return [
        TextDrop(
            reason=reason,  # type: ignore[arg-type]
            action="dropped",
            text=op_text(ops[index], lane),
            op=str(ops[index].get("op") or "") or None,
        )
        for index in sorted(dropped)
    ]
