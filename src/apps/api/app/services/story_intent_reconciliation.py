"""Reconcile derived story rows only through checked, revision-bound references."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Literal

from app.schemas.clip_intents import ClipIntent
from app.schemas.story_intents import StoryIntentRefs


def story_revision(rows: Sequence[str]) -> str:
    return _digest(list(rows))


def inventory_revision(intents: Sequence[ClipIntent]) -> str:
    return _digest(
        sorted((intent.model_dump(mode="json") for intent in intents), key=lambda i: i["intent_id"])
    )


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True)
class StoryReconciliation:
    rows: tuple[str, ...]
    refs: StoryIntentRefs | None
    status: Literal["unchanged", "reconciled", "unresolved"]
    reason: str | None = None
    removed_row_indexes: tuple[int, ...] = ()


def reconcile_story_intents(
    rows: Sequence[str],
    *,
    refs: StoryIntentRefs | None,
    checked_intents: Sequence[ClipIntent],
    rejected_intents: Sequence[ClipIntent],
) -> StoryReconciliation:
    """The caller supplies only validated authoritative rejections, never inferred absence.

    References must originate with the checked story, not be inferred from its text.
    Literal rows describe creator copy and do not assert visual fulfillment.
    Unresolved results preserve the entire input, including otherwise removable rows.
    """
    original = StoryReconciliation(tuple(rows), refs, "unchanged")
    if not rejected_intents:
        return original

    def unresolved(reason: str) -> StoryReconciliation:
        return replace(original, status="unresolved", reason=reason)

    checked = {intent.intent_id: intent for intent in checked_intents}
    if len(checked) != len(checked_intents):
        return unresolved("duplicate_checked_identity")
    if any(checked.get(intent.intent_id) != intent for intent in rejected_intents):
        return unresolved("rejection_not_checked")
    if refs is None:
        return unresolved("missing_references")
    if refs.story_revision != story_revision(rows):
        return unresolved("stale_story_revision")
    if refs.inventory_revision != inventory_revision(checked_intents):
        return unresolved("stale_inventory_revision")
    by_row = {ref.row_index: ref for ref in refs.rows}
    if len(by_row) != len(refs.rows) or set(by_row) != set(range(len(rows))):
        return unresolved("incomplete_row_references")
    rejected_ids = {intent.intent_id for intent in rejected_intents}
    removed = []
    for index in range(len(rows)):
        ref = by_row[index]
        ids = set(ref.intent_ids)
        if ref.kind not in {"derived", "literal"} or ids - checked.keys():
            return unresolved("invalid_row_reference")
        if ref.kind == "literal":
            if (
                not ids
                or ids & rejected_ids
                or any(
                    checked[intent_id].creator_text != rows[index]
                    or checked[intent_id].op not in {"caption", "label"}
                    for intent_id in ids
                )
            ):
                return unresolved("unverified_literal_row")
            continue
        if not ids:
            return unresolved("unmapped_derived_row")
        if ids & rejected_ids:
            if ids - rejected_ids:
                return unresolved("mixed_row")
            removed.append(index)
    kept = tuple(index for index in range(len(rows)) if index not in removed)
    reconciled = tuple(rows[index] for index in kept)
    updated_refs = replace(
        refs,
        story_revision=story_revision(reconciled),
        rows=tuple(replace(by_row[index], row_index=new) for new, index in enumerate(kept)),
    )
    return StoryReconciliation(
        reconciled, updated_refs, "reconciled", removed_row_indexes=tuple(removed)
    )
