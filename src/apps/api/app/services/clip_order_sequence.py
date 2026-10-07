"""The creator's stated clip sequence ("start with X, then Y, end with Z") as ONE rule.

A resolved ``order`` clip intent with a ``first`` / ``last`` position (or neither, "then Y")
and no ``order_by`` describes where a group of clips sits on top of the brief's basis order
(filming time). Two components must agree on where those clips land or the render is a
dead end: the unified montage planner that lays the clips out, and the render contract that
pins the order the finished edit is verified against (KRI-503: the planner honoured "the
blue video first" while the contract still pinned pure filming order, so the edit that did
what the creator said was refused after rendering). Both read the seating from here.

Pure functions over ids: no clips, no settings, no I/O.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable, Mapping, Sequence
from typing import Any, TypeVar

# (position, name, member ids, status): the order intents, listed order.
SequenceRow = tuple[str | None, str, list[str], str]

T = TypeVar("T")


def _nfc(value: object) -> str:
    return unicodedata.normalize("NFC", str(value or "")).strip()


def _members(intent: Mapping[str, Any]) -> list[str]:
    return [
        str(a["media_id"])
        for a in intent.get("assignments") or []
        if isinstance(a, Mapping) and a.get("media_id")
    ]


def sequence_rows(resolved_intents: object) -> list[SequenceRow]:
    """The creator's stated sequence: (position, name, member ids, status), listed order.

    Only ``order`` intents with a ``first`` / ``last`` position, or no position and no
    ``order_by`` ("then the beach volleyball"), describe a sequence of groups. A basis
    order (``order_by``: capture time / route) is the brief's, not this one's.
    """
    rows: list[SequenceRow] = []
    for intent in resolved_intents if isinstance(resolved_intents, list) else []:
        if not isinstance(intent, Mapping) or intent.get("op") != "order":
            continue
        if intent.get("order_by") or intent.get("placeholder"):
            continue
        position = intent.get("position")
        if position not in (None, "first", "last"):
            continue
        status = str(intent.get("status") or "resolved")
        members = _members(intent) if status == "resolved" else []
        rows.append((position, _nfc(intent.get("attribute")), members, status))
    return rows


def apply_sequence(
    ordered: Sequence[T],
    rows: Sequence[SequenceRow],
    key: Callable[[T], str] | None = None,
) -> list[T]:
    """Seat the described groups: ``first`` ones lead, ``last`` ones close, the rest follow.

    A group is the items the server matched to the creator's words (clip facts), kept in
    the order they already had. An item named by two groups belongs to the first one. Items
    no group names keep their relative order between the leading and the closing groups.
    """
    ident: Callable[[T], str] = key or (lambda item: str(item))
    claimed: set[str] = set()
    buckets: dict[str, list[T]] = {"first": [], "mid": [], "last": []}
    refs = {ident(item) for item in ordered}
    for position, _name, members, _status in rows:
        wanted = {m for m in members if m in refs and m not in claimed}
        claimed |= wanted
        buckets[position or "mid"].extend(item for item in ordered if ident(item) in wanted)
    if not claimed:
        return list(ordered)
    rest = [item for item in ordered if ident(item) not in claimed]
    return [*buckets["first"], *buckets["mid"], *rest, *buckets["last"]]
