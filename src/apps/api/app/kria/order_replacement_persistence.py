"""Canonical order supersession authorized by persisted creator choice events."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.kria.order_replacement import (
    CheckedOrderMoment,
    OrderReplacementProof,
    OrderReplacementProposal,
    validate_confirmation,
)
from app.models import CreationThread, CreationThreadEvent, CreatorAgentTurn
from app.services.clip_selection import answered_rows, fold_clip_selections

if TYPE_CHECKING:
    from app.kria.brief import BriefRequirement, BriefUpdate, CreativeBrief
    from app.kria.prepared_execution import SourceIdentity
    from app.services.phone_sources import PhoneSourceBinding


@dataclass(frozen=True)
class OrderReplacementWriteContext:
    creator_id: uuid.UUID
    proof: OrderReplacementProof
    checked_moments: tuple[CheckedOrderMoment, ...]
    sources: tuple[SourceIdentity, ...]
    phone_bindings: tuple[PhoneSourceBinding, ...] = ()


def requirement_digest(requirement: BriefRequirement) -> str:
    return hashlib.sha256(
        json.dumps(
            requirement.model_dump(mode="json"),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def _target(prior: CreativeBrief, proof: OrderReplacementProof) -> BriefRequirement:
    orders = [req for req in prior.live() if req.kind == "order"]
    if (
        len(orders) != 1
        or orders[0].scope != "global"
        or orders[0].literal is not None
        or orders[0].id != proof.proposal.predecessor_requirement_id
        or prior.version != proof.proposal.predecessor_brief_version
        or requirement_digest(orders[0]) != proof.proposal.predecessor_requirement_digest
    ):
        raise ValueError("Order replacement requires its unchanged single global predecessor")
    return orders[0]


def apply_order_replacement(
    prior: CreativeBrief,
    updates: list[BriefUpdate],
    *,
    proof: OrderReplacementProof,
    source_turn_id: str,
    creator_text: str | None = None,
) -> CreativeBrief:
    """Pure canonical transformation; persistence must validate creator authority first."""
    from app.kria.brief import BriefRequirement, CreativeBrief, _next_id_number, apply_updates

    target = _target(prior, proof)
    if any(
        update.target_requirement_id == target.id or update.kind == "order" for update in updates
    ):
        raise ValueError("Ordinary order updates conflict with the confirmed replacement")
    merged = apply_updates(prior, updates, source_turn_id=source_turn_id, creator_text=creator_text)
    replacement = BriefRequirement(
        id=f"r{_next_id_number(merged.requirements)}",
        kind="order",
        scope="global",
        description=(
            f"The {len(proof.proposal.moments)} items you confirmed, in the displayed order."
        ),
        source_turn_id=source_turn_id,
        order_replacement=proof,
    )
    rows = [
        req.model_copy(update={"status": "superseded"}) if req.id == target.id else req
        for req in merged.requirements
    ]
    return CreativeBrief(version=prior.version, requirements=[*rows, replacement])


def check_replacement_retry(
    existing: CreativeBrief,
    context: OrderReplacementWriteContext,
    *,
    db: Any,
    thread_id: uuid.UUID,
    turn_id: uuid.UUID,
) -> None:
    thread = db.get(CreationThread, thread_id)
    turn = db.get(CreatorAgentTurn, turn_id)
    if (
        thread is None
        or thread.creator_id != context.creator_id
        or turn is None
        or turn.thread_id != thread_id
    ):
        raise ValueError("Order replacement retry does not belong to this creator's thread")
    matches = [
        req
        for req in existing.requirements
        if req.order_replacement is not None and req.source_turn_id == str(turn_id)
    ]
    if len(matches) != 1 or matches[0].order_replacement != context.proof:
        raise ValueError("This turn already persisted a different order replacement")


def _current_rejection(
    events: list[CreationThreadEvent], proposal: OrderReplacementProposal, question_sequence: int
) -> None:
    questions: dict[str, dict] = {}
    count = 0
    last_sequence = None
    category = None
    for event in events:
        payload = event.payload or {}
        question = payload.get("clip_question") if event.role == "assistant" else None
        if isinstance(question, dict):
            questions[str(question.get("question_id"))] = question
        selection = payload.get("clip_selection") if event.role == "user" else None
        if not isinstance(selection, dict) or str(selection.get("question_id")) not in questions:
            continue
        count += 1
        last_sequence = event.sequence
        asked = questions[str(selection.get("question_id"))]
        if selection.get("question_id") == proposal.rejection_question_id:
            category = next(
                (
                    row
                    for parent in asked.get("categories") or []
                    if isinstance(parent, dict)
                    for row in answered_rows(parent)
                    if row.get("key") == proposal.rejection_category_key
                ),
                None,
            )
    entry = fold_clip_selections([(event.role, event.payload) for event in events]).by_key.get(
        proposal.rejection_category_key
    )
    if (
        entry is None
        or not entry.none
        or entry.question_id != proposal.rejection_question_id
        or entry.category_key != proposal.rejection_category_key
        or entry.seq != proposal.rejection_selection_sequence
        or count != entry.seq
        or last_sequence is None
        or last_sequence >= question_sequence
        or category is None
        or category.get("intent_id") != proposal.rejected_intent_id
        or category.get("op") != "order"
    ):
        raise ValueError("Order replacement rejection was not the current checked clip answer")


def validate_persisted_replacement(
    db: Any,
    *,
    thread_id: uuid.UUID,
    turn_id: uuid.UUID,
    prior: CreativeBrief | None,
    context: OrderReplacementWriteContext,
) -> OrderReplacementProof:
    """Re-read private offer and source answer under the caller's thread lock."""
    proof = context.proof
    if prior is None:
        raise ValueError("Order replacement has no canonical predecessor")
    target = _target(prior, proof)
    thread = db.get(CreationThread, thread_id)
    turn = db.get(CreatorAgentTurn, turn_id)
    if (
        thread is None
        or thread.creator_id != context.creator_id
        or turn is None
        or turn.thread_id != thread_id
    ):
        raise ValueError("Order replacement turn does not belong to this creator's thread")
    events = list(
        db.execute(
            select(CreationThreadEvent)
            .where(CreationThreadEvent.thread_id == thread_id)
            .order_by(CreationThreadEvent.sequence)
        ).scalars()
    )
    answer = next((event for event in events if event.id == turn.source_event_id), None)
    if (
        answer is None
        or answer.role != "user"
        or answer.sequence != proof.selection_sequence
        or any(event.role == "user" and event.sequence > answer.sequence for event in events)
    ):
        raise ValueError("Order replacement answer is not the current user turn")
    offered = [
        event
        for event in events
        if event.role == "assistant"
        and isinstance((event.payload or {}).get("choice_question"), dict)
        and event.payload["choice_question"].get("question_id") == proof.question_id
    ]
    if len(offered) != 1 or offered[0].sequence != proof.question_sequence:
        raise ValueError("Order replacement question is missing or ambiguous")
    event = offered[0]
    question = event.payload["choice_question"]
    try:
        question_turn_id = uuid.UUID(event.payload.get("turn_id", ""))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Order replacement question lacks its server turn") from None
    question_turn = db.get(CreatorAgentTurn, question_turn_id)
    if (
        question_turn is None
        or question_turn.thread_id != thread_id
        or question_turn.status != "completed"
    ):
        raise ValueError("Order replacement question has no completed same-thread server turn")
    question_source = next((row for row in events if row.id == question_turn.source_event_id), None)
    if (
        question_source is None
        or question_source.role != "user"
        or question_source.sequence >= event.sequence
    ):
        raise ValueError("Order replacement question source is not an earlier creator turn")
    saved = question_turn.plan_json or {}
    proposal = OrderReplacementProposal.model_validate(saved.get("order_replacement_proposal"))
    if proposal != proof.proposal or saved.get("choice_question") != question:
        raise ValueError("Order replacement disagrees with the saved server offer")
    for earlier in events:
        selection = (earlier.payload or {}).get("choice_selection")
        if (
            earlier.role == "user"
            and earlier.id != answer.id
            and isinstance(selection, dict)
            and selection.get("question_id") == proof.question_id
        ):
            raise ValueError("Order replacement question has already been answered")
    _current_rejection(events, proposal, event.sequence)
    validated = validate_confirmation(
        proposal,
        question=question,
        selection=(answer.payload or {}).get("choice_selection") or {},
        question_sequence=event.sequence,
        selection_sequence=answer.sequence,
        predecessor_requirement_id=target.id,
        predecessor_brief_version=prior.version,
        predecessor_requirement_digest=requirement_digest(target),
        checked_moments=context.checked_moments,
        sources=context.sources,
        phone_bindings=context.phone_bindings,
        rejection_question_id=proposal.rejection_question_id,
        rejection_category_key=proposal.rejection_category_key,
        rejection_selection_sequence=proposal.rejection_selection_sequence,
        rejected_intent_id=proposal.rejected_intent_id,
    )
    if validated != proof:
        raise ValueError("Order replacement proof does not match the persisted choice")
    return validated
