"""Validated creator omissions shared by draft receipts and montage records."""

from collections.abc import Mapping
from typing import Any

from pydantic import TypeAdapter

from app.schemas.story_intents import SelectionReconciliation


def creator_omission_outcomes(strategy: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = strategy.get("selection_reconciliation")
    if raw is None:
        return []
    reconciliation = TypeAdapter(SelectionReconciliation).validate_python(raw)
    executed_ids = {
        row.get("intent_id")
        for row in strategy.get("resolved_clip_intents") or []
        if isinstance(row, Mapping)
    }
    outcomes: dict[str, dict[str, Any]] = {}
    for selection in reconciliation.rejected_selections:
        intent = selection.intent
        if intent.op != "order":
            continue
        if (
            not selection.question_id
            or not selection.category_key
            or selection.selection_sequence < 1
        ):
            raise ValueError("Creator omission requires authoritative selection provenance")
        if intent.intent_id in executed_ids:
            raise ValueError("Creator-rejected order intent remains executable")
        row = {
            "intent_id": intent.intent_id,
            "op": "order",
            "attribute": intent.attribute,
            "name": intent.attribute,
            "status": "unresolved",
            "code": "skipped",
            "reason": "creator selected None",
        }
        if intent.intent_id in outcomes and outcomes[intent.intent_id] != row:
            raise ValueError("Creator omission identity has conflicting referents")
        outcomes[intent.intent_id] = row
    return list(outcomes.values())
