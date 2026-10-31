"""A first / last clip the creator has since left out comes off the brief (KRI-1111).

Leverkusen–Nürnberg M3 (thread 83f2dcaa, 2026-10-31): "Start with my ticket" stored
``first_clip: "my ticket"`` on the order requirement. The creator then said "Then leave the
ticket out." (13:20) and "Leave the ticket out completely." (13:26). The Main Creator changed
the order requirement on both turns, but a change that leaves a sticky fact out carries it
back (``brief._carry_sticky_facts``, KRI-522), so all eight brief versions kept it. The clip
planner, which reads every creator message, wrote a leave-out of "the ticket" and no opening
on both turns; the server added the opening back from the brief (KRI-1067) and asked the
creator which clip shows "my ticket", pre-ticking a light-show video.

``clip_intent_planning.excluded_anchors`` finds such an end (the leave-out names exactly its
words, articles and "my" / "your" aside) and keeps it off the clip picker. Here the same
turn's brief updates drop it as KRI-1068 does for "skip that": a null on the order
requirement, keeping the Main Creator's own change of it (its new ending). Otherwise the next
draft's render contract would still refuse on the brief's words ("I couldn't tell which clip
"my ticket" means").

Pure function over the brief and the turn's updates: no I/O, no model.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from types import SimpleNamespace
from typing import Any

from app.kria.brief import (
    STICKY_FACTS,
    BriefUpdate,
    BriefUpdateBatchError,
    CreativeBrief,
    apply_updates,
)
from app.services.clip_none_answers import _FACT_OF, _content, _order_basis, _pin_notice
from app.services.pin_withdrawal import PinWithdrawal, _withdrawal_update


def _holds(facts: Mapping[str, object] | None, position: str, words: str) -> bool:
    said = _content(words)
    return bool(said) and _content((facts or {}).get(_FACT_OF[position])) == said


def withdraw_excluded_anchors(
    prior: CreativeBrief | None,
    updates: Iterable[BriefUpdate],
    excluded: Mapping[str, str],
) -> PinWithdrawal:
    """This turn's brief updates without the first / last clips in ``excluded``.

    ``excluded`` is ``{position: the brief's words}`` from ``excluded_anchors``. An order
    requirement the brief already holds gets a change with that fact set to null (on top of
    the model's own change of it this turn, if any; its remove stands). One this turn adds
    is added without it. A clip the brief no longer names this way changes nothing.
    """
    updates = tuple(updates)
    unchanged = PinWithdrawal(updates)
    if not excluded or prior is None:
        return unchanged
    try:
        after = apply_updates(prior, updates, source_turn_id=None)
    except BriefUpdateBatchError:
        return unchanged  # the batch is refused downstream as before
    known = {req.id for req in prior.live()}
    gone: dict[str, dict[str, str]] = {}
    for req in after.live():
        if req.kind != "order" or req.id not in known:
            continue
        for position, words in excluded.items():
            if _holds(req.facts, position, words):
                gone.setdefault(req.id, {})[position] = words
    out: list[BriefUpdate] = []
    notices: list[str] = []
    dropped_from_adds = False
    for update in updates:
        if update.operation == "add" and update.kind == "order":
            pins = {p: w for p, w in excluded.items() if _holds(update.facts, p, w)}
            if pins:
                drop = {_FACT_OF[position] for position in pins}
                facts = {k: v for k, v in update.facts.items() if k not in drop}
                out.append(update.model_copy(update={"facts": facts}))
                notices.extend(_notices(pins, SimpleNamespace(facts=update.facts)))
                dropped_from_adds = True
                continue
        if update.operation != "add" and update.target_requirement_id in gone:
            continue  # replaced below, its own change carried over
        out.append(update)
    if not gone and not dropped_from_adds:
        return unchanged
    before = {req.id: req for req in prior.live()}
    for req_id, pins in gone.items():
        req = next(r for r in after.live() if r.id == req_id)
        update = _withdrawal_update(req, pins, prior.version)
        if update.operation == "change":
            # A sticky fact the model's own change withdrew this turn stays withdrawn: left
            # out of this replacement it would be carried back from the prior version.
            dropped = STICKY_FACTS & before[req_id].facts.keys() - update.facts.keys()
            if dropped:
                facts = {**update.facts, **dict.fromkeys(dropped)}
                update = update.model_copy(update={"facts": facts})
        out.append(update)
        notices.extend(_notices(pins, req))
    return PinWithdrawal(tuple(out), tuple(dict.fromkeys(notices)))


def _notices(pins: Mapping[str, str], req: Any) -> list[str]:
    """What the draft (or the reply holding it back) says: the KRI-1068 "As you asked" line."""
    basis = _order_basis(req)
    return [
        _pin_notice(position, words, basis, skipped=False, withdrawn=True)
        for position, words in pins.items()
    ]
