"""Server-built, verifiable repair of a finished generation (KRI-1171).

A repair is never a model output. It is the compiled result of catalog candidates whose own
precondition held (`intent_review_catalog`), staged as an ordinary reversible draft that waits for
the creator's approval. This module holds the parts that need no database: the operation bundle,
the in-transaction fences `_complete_draft_turn` applies, and the allow-list diff that refuses any
compiled draft that touched more than the candidates named.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.kria.intent_review_catalog import Candidate
from app.kria.intent_review_disposition import MAX_REPAIR_CANDIDATES, Blocked
from app.kria.planner import PlannedKriaTurn, adapt_editor_action
from app.services.kria_editor_ops_text import clean_authored_text

INTENT_REPAIR_EVENT = "intent_review_repair"
AUTHORITY_KIND = "instruction_preserving_v1"
_TIMING_TOLERANCE_S = 1e-6
_NUMERIC_FIELDS = frozenset({"start_s", "end_s"})


class IntentRepairRefused(Exception):
    """The repair cannot be staged as asked; `reason` is the disposition `why` it becomes."""

    def __init__(self, reason: Blocked):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class IntentRepairSpec:
    review_id: str
    candidate_ids: tuple[str, ...]
    base_draft_id: str
    base_draft_revision: int
    base_snapshot_hash: str
    variant_id: str
    render_generation_id: str
    expected: Mapping[str, Mapping[str, Any]]
    authority: Mapping[str, Any] = field(default_factory=dict)

    def allowed_fields(self) -> dict[str, frozenset[str]]:
        allowed: dict[str, frozenset[str]] = {}
        for element, values in self.expected.items():
            names = set(values)
            if "\n" in str(values.get("text") or ""):
                names.add("wrap_lines")
            allowed[element] = frozenset(names)
        return allowed


def build_repair_ops(
    candidates: Sequence[Candidate],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Operations in the coerced shape the compiler re-resolves, and the values they must yield."""
    if not 0 < len(candidates) <= MAX_REPAIR_CANDIDATES:
        raise IntentRepairRefused("protected")
    ops: list[dict[str, Any]] = []
    expected: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        if not candidate.auto:
            raise IntentRepairRefused("protected")
        addressed = {
            "selector": {"ids": [candidate.element_ref]},
            "target_ids": [candidate.element_ref],
            "expected_count": 1,
        }
        if candidate.kind == "restore_exact_text":
            text = candidate.target.get("text")
            if not isinstance(text, str) or not text or clean_authored_text(text) != text:
                raise IntentRepairRefused("protected")
            ops.append({"op": "rewrite_text", "text": text, **addressed})
            fields: dict[str, Any] = {"text": text}
        elif candidate.kind == "fit_text_to_duration":
            start, end = candidate.target.get("start_s"), candidate.target.get("end_s")
            if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (start, end)):
                raise IntentRepairRefused("protected")
            if end <= start:
                raise IntentRepairRefused("protected")
            ops.append({"op": "set_texts_timing", "start_s": start, "end_s": end, **addressed})
            fields = {"start_s": float(start), "end_s": float(end)}
        else:
            raise IntentRepairRefused("protected")
        expected.setdefault(candidate.element_ref, {}).update(fields)
    return ops, expected


def planned_repair_turn(
    *,
    spec: IntentRepairSpec,
    ops: list[dict[str, Any]],
    summary: str,
    manifest_hash: str | None,
    context_hash: str | None,
    client_protocol: int | None,
) -> PlannedKriaTurn:
    plan = adapt_editor_action(reply=summary, ops=ops, request_render=True)
    if plan.mode != "act" or [i.tool_name for i in plan.intents] != [
        "draft.apply_editor_ops",
        "render.request",
    ]:
        raise IntentRepairRefused("protected")
    return PlannedKriaTurn(
        plan=plan,
        manifest_hash=manifest_hash,
        context_hash=context_hash,
        client_protocol=client_protocol,
        intent_repair=spec,
    )


def guard_intent_repair_head(
    spec: IntentRepairSpec | None,
    *,
    head: Any,
    variant: Mapping[str, Any],
    pending_approvals: int,
) -> None:
    """Refuse inside the draft transaction once the creator's draft is not the approved one."""
    if spec is None:
        return
    if (
        head is None
        or str(head.id) != spec.base_draft_id
        or int(head.draft_revision) != spec.base_draft_revision
        or head.snapshot_hash != spec.base_snapshot_hash
        or variant.get("render_status") != "ready"
        or str(variant.get("render_generation_id") or "") != spec.render_generation_id
        or str(variant.get("variant_id") or "") != spec.variant_id
    ):
        raise IntentRepairRefused("head_moved")
    if pending_approvals:
        raise IntentRepairRefused("thread_busy")


def _by_id(rows: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(rows, list):
        raise IntentRepairRefused("protected")
    keyed: dict[str, dict[str, Any]] = {}
    for row in rows:
        identifier = row.get("id") if isinstance(row, dict) else None
        if not isinstance(identifier, str) or identifier in keyed:
            raise IntentRepairRefused("protected")
        keyed[identifier] = row
    return keyed


def _same(name: str, left: Any, right: Any) -> bool:
    if name in _NUMERIC_FIELDS:
        try:
            return abs(float(left) - float(right)) <= _TIMING_TOLERANCE_S
        except (TypeError, ValueError):
            return False
    return left == right


def verify_intent_repair(
    spec: IntentRepairSpec | None, *, before: Mapping[str, Any], payload: Any
) -> None:
    """The compiled draft changed exactly what the candidates named and nothing else."""
    if spec is None:
        return
    after = (
        payload.model_dump(mode="json", exclude_none=True)
        if hasattr(payload, "model_dump")
        else payload
    )
    if not isinstance(after, dict) or "text_elements" not in before:
        raise IntentRepairRefused("protected")
    if set(before) - {"text_elements"}:
        raise IntentRepairRefused("protected")
    old, new = _by_id(before["text_elements"]), _by_id(after.get("text_elements"))
    if list(old) != list(new):
        raise IntentRepairRefused("protected")
    allowed = spec.allowed_fields()
    changed: set[str] = set()
    for identifier, row in new.items():
        names = {
            name
            for name in row.keys() | old[identifier].keys()
            if row.get(name) != old[identifier].get(name)
        }
        if not names:
            continue
        if identifier not in allowed or not names <= allowed[identifier]:
            raise IntentRepairRefused("protected")
        changed.add(identifier)
    for identifier, values in spec.expected.items():
        if identifier not in new or identifier not in changed:
            raise IntentRepairRefused("protected")
        for name, value in values.items():
            if name not in new[identifier] or not _same(name, new[identifier][name], value):
                raise IntentRepairRefused("protected")
            if isinstance(value, str) and new[identifier][name].encode() != value.encode():
                raise IntentRepairRefused("protected")


def applied_intent_repair_receipt(spec: IntentRepairSpec | None) -> dict[str, Any]:
    if spec is None:
        return {}
    return {
        "intent_repair": {
            "review_id": spec.review_id,
            "candidate_ids": list(spec.candidate_ids),
            "base_draft_id": spec.base_draft_id,
            "base_draft_revision": spec.base_draft_revision,
            "base_snapshot_hash": spec.base_snapshot_hash,
            "authority": dict(spec.authority),
        }
    }
