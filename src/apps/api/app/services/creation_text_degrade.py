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
from typing import TYPE_CHECKING, Any, Generic, Literal, TypeVar, get_args

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


@dataclass(frozen=True)
class TextDrop:
    """One item the text pass left out or did only in part, and why."""

    reason: TextGateReason
    action: Literal["dropped", "shrunk"]
    # The creator-visible words the drop concerns (quoted in the note), when there are some.
    # For `incomplete` it is her unmet ask in her words, as the composer restated it.
    text: str | None = None
    # The dropped editor op's name, for the admin trace only.
    op: str | None = None
    # Whether the item may be one the creator asked for. For `unasked_text`: one of her live
    # text asks could own the line. A title ask ("and the English translation at the bottom")
    # authorizes no new row, yet the line is hers (prod 3c312af4), so leaving it out is said.
    # For `incomplete`: False when no ask of hers is unmet and only composer ops the parser
    # refused are missing, so the note does not call them part of her request.
    asked: bool = True

    def note(self) -> str | None:
        """The "What Kria changed" line, in the bound reply language; None = nothing to say.

        A line no text ask could own was never the creator's, so dropping it is silent, as
        ``_drop_unasked_added_text`` already is.
        """
        from app.pipeline.phone_plan_repair import _quoted  # noqa: PLC0415

        quoted = _quoted(self.text) if self.text and self.text.strip() else None
        if self.reason == "unasked_text":
            if not self.asked:
                return None
            if quoted is None:
                return say(
                    en="Left out one line I couldn't add to your text",
                    tr="Yazına ekleyemediğim bir satırı çıkardım",
                )
            return say(
                en="Left out {text} because I couldn't add it as its own line",
                tr="{text} metnini ayrı bir satır olarak ekleyemediğim için çıkardım",
                text=quoted,
            )
        if self.reason == "incomplete":
            if quoted is not None:
                return say(
                    en="I couldn't do {text}, so the video shows the rest of your text",
                    tr="{text} isteğini yapamadım, videoda yazının geri kalanı var",
                    text=quoted,
                )
            if not self.asked:
                return say(
                    en="Left out one text change I couldn't make",
                    tr="Yapamadığım bir yazı değişikliğini eklemedim",
                )
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

    def record(self) -> dict[str, Any]:
        """What the finished-render review needs to say this drop again in the chat's language."""
        return {
            "reason": self.reason,
            "action": self.action,
            "asked": self.asked,
            **({"text": self.text[:160]} if self.text else {}),
        }


# Drops whose note is "Left out one text change for ... because I couldn't make it fit". Past
# `_FIT_LINES` of them (a composer that mistimed every label), they share one counted line.
_FIT_REASONS = frozenset({"replay_failed", "research_missing", "layer_shape"})
_FIT_LINES = 3


def text_drop_notes(drops: Sequence[TextDrop]) -> list[str]:
    """Each drop's note once, in order, in the caller's bound reply language."""
    fit = [
        index
        for index, drop in enumerate(drops)
        if drop.reason in _FIT_REASONS and drop.action == "dropped"
    ]
    notes: list[str] = []
    for index, drop in enumerate(drops):
        if len(fit) <= _FIT_LINES or index not in fit:
            note = drop.note()
        elif index == fit[0]:
            note = say(
                en="Left out {count} text changes I couldn't make fit",
                tr="Sığdıramadığım {count} yazı değişikliğini eklemedim",
                count=len(fit),
            )
        else:
            continue
        if note and note[:160] not in notes:
            notes.append(note[:160])
    return notes


# The unified montage record's key for what its text pass left out (generation-scoped).
TEXT_DROPS_FIELD = "text_drops"


def text_drops_record(generation: str | None, drops: Sequence[TextDrop]) -> dict[str, Any]:
    return {"generation_id": generation, "drops": [drop.record() for drop in drops]}


def finished_render_notes(
    assembly_plan: Mapping[str, Any], generations: set[str | None]
) -> list[str]:
    """The text-pass notes for a finished render, in the bound reply language (KRI-1376).

    Read off the unified montage record of the plan this render was laid out from, so a later
    edit (its own generation) never repeats them. The variant's ``phone_repair_notes`` hold the
    same lines, but no client shows them after approval (KRI-1261).
    """
    record = assembly_plan.get("unified_montage")
    entry = record.get(TEXT_DROPS_FIELD) if isinstance(record, Mapping) else None
    if not isinstance(entry, Mapping) or entry.get("generation_id") not in generations:
        return []
    drops = []
    for row in entry.get("drops") or ():
        if not isinstance(row, Mapping) or row.get("reason") not in get_args(TextGateReason):
            continue
        drops.append(
            TextDrop(
                reason=row["reason"],
                action="shrunk" if row.get("action") == "shrunk" else "dropped",
                text=row.get("text") if isinstance(row.get("text"), str) else None,
                asked=row.get("asked") is not False,
            )
        )
    return text_drop_notes(drops)


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


def shrink(
    error: CreationTextCompositionError, *, text: str | None = None, asked: bool = True
) -> TextDrop:
    """Render without the one asked behavior ``error`` names, or re-raise it when it is not one
    of the gates that may (``_SHRINKABLE``). ``text`` and ``asked`` are the drop's own."""
    if error.reason not in _SHRINKABLE:
        raise error
    return TextDrop(reason=error.reason, action="shrunk", text=text, asked=asked)  # type: ignore[arg-type]


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
    text_asked: bool = True,
) -> Degraded[T]:
    """``attempt(ops)``, narrowing ``ops`` one rejected item at a time until it passes.

    ``ops`` are the composer's own ops, the only ones a degrade may drop (approved research,
    seeded words and server-timed sequences are added inside ``attempt``). Each round tries
    every single drop (or the unasked lines, together) and keeps the first that passes. When no
    single drop passes, the round still narrows: by a drop that turns the rejection into a
    different degradable one, else by the first op whose arrival makes a prefix of the program
    fail the same way (several equally bad rows). Every such round removes at least one op, so
    there are at most as many rounds as ops; each costs replays only, never a model call. A
    timeout, an untyped rejection, or a round with nothing left to try re-raises the first
    rejection unchanged.

    ``text_asked``: the creator has a live text ask, so an ``unasked_text`` line may be hers
    and leaving it out is said (``TextDrop.asked``).
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

    def dropped_items(reason: str | None, indexes: frozenset[int]) -> list[TextDrop]:
        return _dropped(reason, kept, indexes, lane, asked=text_asked)

    for _ in range(len(ops)):
        strategy = _STRATEGY.get(error.reason or "")
        if strategy is None:
            raise first
        progress: tuple[frozenset[int], CreationTextCompositionError] | None = None
        # What each single drop still fails with: the next round's rejection after a culprit
        # found by `_first_failing` (its own single drop was tried here).
        failures: dict[frozenset[int], CreationTextCompositionError] = {}
        for dropped in _candidates(strategy, kept, protected, named=error.op_index):
            trial = [op for index, op in enumerate(kept) if index not in dropped]
            try:
                result = attempt(trial)
            except CreationTextTimeoutError:
                raise
            except CreationTextCompositionError as exc:
                failures[dropped] = exc
                if progress is None and _key(exc) != _key(error) and exc.reason in _STRATEGY:
                    progress = (dropped, exc)
                continue
            drops.extend(dropped_items(error.reason, dropped))
            stand_ins = _stand_ins(kept, dropped, lane, protected)
            if stand_ins:
                # The composer moved the confirmed text and reworded it on the way: the reworded
                # line goes too, so no invented words stay next to the creator's. It is the same
                # change the copy note already names.
                wider = [
                    op for index, op in enumerate(kept) if index not in dropped | set(stand_ins)
                ]
                try:
                    result, trial = attempt(wider), wider
                    drops.extend(
                        TextDrop(reason="copy_changed", action="dropped", text=text, op="add_text")
                        for _index, text in sorted(stand_ins.items())
                    )
                except CreationTextCompositionError:
                    pass
            return Degraded(result, trial, drops, first)
        if progress is None and strategy == "one_op":
            culprit = _first_failing(kept, attempt, error)
            remaining = failures.get(frozenset({culprit})) if culprit is not None else None
            if remaining is not None and remaining.reason in _STRATEGY:
                progress = (frozenset({culprit}), remaining)
        if progress is None:
            raise first
        dropped, next_error = progress
        drops.extend(dropped_items(error.reason, dropped))
        kept = [op for index, op in enumerate(kept) if index not in dropped]
        error = next_error
    raise first


def _key(exc: CreationTextCompositionError) -> tuple[str | None, str | None, str]:
    return exc.reason, exc.item_id, str(exc)


def _first_failing(
    ops: list[dict],
    attempt: Callable[[list[dict]], Any],
    error: CreationTextCompositionError,
) -> int | None:
    """The op whose arrival first makes the program fail exactly as ``error`` does.

    Two rows bad the same way survive every single drop (the other one still fails), so the
    shortest failing prefix names one of them. An op only reads ops before it, so a prefix is a
    program of its own. A prefix that fails differently (half of a move) is not the culprit,
    and when the program fails that way with none of the ops, no op is.
    """
    from app.services.creation_text_composition import (  # noqa: PLC0415
        CreationTextCompositionError,
        CreationTextTimeoutError,
    )

    for end in range(len(ops)):
        try:
            attempt(ops[:end])
        except CreationTextTimeoutError:
            raise
        except CreationTextCompositionError as exc:
            if _key(exc) == _key(error):
                return end - 1 if end else None
    return len(ops) - 1 if ops else None


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
) -> dict[int, str]:
    """The added lines that only reword the confirmed text a dropped removal took off screen,
    each with the text it rewords.

    A rewording shares at least three quarters of its words with the removed row, both ways
    ("Oblea de la casa (nueva)" for "Oblea de la casa"). A separate line that only mentions
    the row ("Caro brought us the oblea de la casa") is not one and stays.
    """
    from app.text_fold import loose_words  # noqa: PLC0415

    removed = [
        (text, set(loose_words(text)))
        for index in sorted(dropped)
        if ops[index].get("op") in _REMOVALS and (text := op_text(ops[index], lane))
    ]
    stand_ins: dict[int, str] = {}
    for index, op in enumerate(ops):
        if index in dropped or op.get("op") != "add_text" or protected(op):
            continue
        words = set(loose_words(str(op.get("text") or "")))
        for text, row in removed:
            shared = len(words & row)
            if row and words and 4 * shared >= 3 * len(row) and 4 * shared >= 3 * len(words):
                stand_ins[index] = text
                break
    return stand_ins


def _dropped(
    reason: str | None,
    ops: list[dict],
    dropped: frozenset[int],
    lane: Sequence[Mapping[str, Any]],
    *,
    asked: bool,
) -> list[TextDrop]:
    reason = reason if reason in _STRATEGY else "replay_failed"
    return [
        TextDrop(
            reason=reason,  # type: ignore[arg-type]
            action="dropped",
            text=op_text(ops[index], lane),
            op=str(ops[index].get("op") or "") or None,
            asked=asked,
        )
        for index in sorted(dropped)
    ]
