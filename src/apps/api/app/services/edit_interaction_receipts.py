"""Append-only persistence for Copilot proposal and browser execution receipts."""

from __future__ import annotations

import hashlib
import inspect
import json
import uuid
from typing import Literal

from fastapi import HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.edit_copilot import EditCopilotAgent
from app.models import EditInteractionReceipt
from app.routes._copilot import CopilotTurnBody, CopilotTurnResponse
from app.services.training_eligibility import evaluate_training_eligibility_async

ExecutionOutcome = Literal["applied", "no_effect", "rejected", "stale", "failed"]


class ExecutionRejectionReason(BaseModel):
    op: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=100)
    detail: str = Field(min_length=1, max_length=500)


class ExecuteCopilotReceiptBody(BaseModel):
    client_event_id: str = Field(min_length=1, max_length=128)
    outcome: ExecutionOutcome
    rejection_reasons: list[ExecutionRejectionReason] = Field(default_factory=list, max_length=100)
    before_revision_hash: str | None = Field(default=None, max_length=128)
    after_revision_hash: str | None = Field(default=None, max_length=128)

    @field_validator("client_event_id")
    @classmethod
    def _strip_client_event_id(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("client_event_id cannot be blank")
        return stripped

    @field_validator("before_revision_hash", "after_revision_hash")
    @classmethod
    def _strip_identifier(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class ExecuteCopilotReceiptResponse(BaseModel):
    receipt_id: str
    execution_receipt_id: str
    client_event_id: str
    recorded: bool


def _operations_digest(ops: list[dict]) -> str:
    encoded = json.dumps(ops, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


async def _add(db: AsyncSession, row: EditInteractionReceipt) -> None:
    """Keep the ORM's synchronous ``add`` compatible with route-test fakes."""

    result = db.add(row)
    if inspect.isawaitable(result):
        await result


def _proposal_before_hash(body: CopilotTurnBody) -> str | None:
    revision = body.snapshot.get("guided_revision")
    if not isinstance(revision, dict):
        return None
    state_hash = revision.get("state_hash")
    if isinstance(state_hash, str) and state_hash.strip():
        return state_hash.strip()[:128]
    number = revision.get("revision_number")
    generation = revision.get("base_generation")
    if isinstance(number, int) and isinstance(generation, str) and generation.strip():
        return f"{number}:{generation.strip()}"[:128]
    return None


async def persist_copilot_proposal(
    db: AsyncSession,
    *,
    creator_id: uuid.UUID,
    plan_item_id: uuid.UUID,
    job_id: uuid.UUID,
    variant_id: str,
    body: CopilotTurnBody,
    response: CopilotTurnResponse,
) -> CopilotTurnResponse:
    """Persist the exact final server response and attach its durable identity."""

    eligibility = await evaluate_training_eligibility_async(db, creator_id)
    if not eligibility.eligible:
        return response
    receipt_id = uuid.uuid4()
    row = EditInteractionReceipt(
        id=receipt_id,
        event_kind="proposal",
        proposal_receipt_id=None,
        creator_id=creator_id,
        plan_item_id=plan_item_id,
        job_id=job_id,
        variant_id=variant_id,
        client_event_id=None,
        utterance=body.message,
        inferred_intent=response.intent,
        model_reply=response.reply,
        eligibility_basis=eligibility.basis,
        consent_event_id=eligibility.consent_event_id,
        internal_grant_id=eligibility.internal_grant_id,
        proposed_operations=list(response.ops),
        proposed_operations_digest=_operations_digest(response.ops),
        prompt_version=EditCopilotAgent.spec.prompt_version,
        model=EditCopilotAgent.spec.model,
        proposal_outcome=response.outcome,
        execution_outcome=None,
        rejection_reasons=list(response.rejection_reasons),
        before_revision_hash=_proposal_before_hash(body),
        after_revision_hash=None,
    )
    await _add(db, row)
    await db.commit()
    return response.model_copy(update={"receipt_id": str(receipt_id)})


def _execution_matches(row: EditInteractionReceipt, body: ExecuteCopilotReceiptBody) -> bool:
    return (
        row.execution_outcome == body.outcome
        and list(row.rejection_reasons or [])
        == [reason.model_dump(mode="json") for reason in body.rejection_reasons]
        and row.before_revision_hash == body.before_revision_hash
        and row.after_revision_hash == body.after_revision_hash
    )


async def _existing_execution(
    db: AsyncSession,
    *,
    creator_id: uuid.UUID,
    client_event_id: str,
) -> EditInteractionReceipt | None:
    return (
        await db.execute(
            select(EditInteractionReceipt).where(
                EditInteractionReceipt.creator_id == creator_id,
                EditInteractionReceipt.client_event_id == client_event_id,
                EditInteractionReceipt.event_kind == "execution",
            )
        )
    ).scalar_one_or_none()


def _idempotent_response(
    proposal_id: uuid.UUID,
    execution: EditInteractionReceipt,
    *,
    recorded: bool,
) -> ExecuteCopilotReceiptResponse:
    return ExecuteCopilotReceiptResponse(
        receipt_id=str(proposal_id),
        execution_receipt_id=str(execution.id),
        client_event_id=str(execution.client_event_id),
        recorded=recorded,
    )


async def persist_copilot_execution(
    db: AsyncSession,
    *,
    proposal_id: uuid.UUID,
    creator_id: uuid.UUID,
    plan_item_id: uuid.UUID,
    variant_id: str,
    body: ExecuteCopilotReceiptBody,
) -> ExecuteCopilotReceiptResponse:
    """Append an actual browser outcome; repeated client events are idempotent."""

    proposal = (
        await db.execute(
            select(EditInteractionReceipt).where(
                EditInteractionReceipt.id == proposal_id,
                EditInteractionReceipt.event_kind == "proposal",
                EditInteractionReceipt.creator_id == creator_id,
                EditInteractionReceipt.plan_item_id == plan_item_id,
                EditInteractionReceipt.variant_id == variant_id,
            )
        )
    ).scalar_one_or_none()
    if proposal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Receipt not found")
    proposal_uuid = proposal.id

    existing = await _existing_execution(
        db, creator_id=creator_id, client_event_id=body.client_event_id
    )
    if existing is not None:
        if existing.proposal_receipt_id != proposal_uuid or not _execution_matches(existing, body):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="client_event_id was already used for a different receipt",
            )
        return _idempotent_response(proposal_uuid, existing, recorded=False)

    execution = EditInteractionReceipt(
        id=uuid.uuid4(),
        event_kind="execution",
        proposal_receipt_id=proposal_uuid,
        creator_id=creator_id,
        plan_item_id=plan_item_id,
        job_id=proposal.job_id,
        variant_id=variant_id,
        client_event_id=body.client_event_id,
        utterance=proposal.utterance,
        inferred_intent=proposal.inferred_intent,
        model_reply=proposal.model_reply,
        eligibility_basis=proposal.eligibility_basis,
        consent_event_id=proposal.consent_event_id,
        internal_grant_id=proposal.internal_grant_id,
        proposed_operations=list(proposal.proposed_operations or []),
        proposed_operations_digest=proposal.proposed_operations_digest,
        prompt_version=proposal.prompt_version,
        model=proposal.model,
        proposal_outcome=proposal.proposal_outcome,
        execution_outcome=body.outcome,
        rejection_reasons=[reason.model_dump(mode="json") for reason in body.rejection_reasons],
        before_revision_hash=body.before_revision_hash,
        after_revision_hash=body.after_revision_hash,
    )
    await _add(db, execution)
    try:
        await db.commit()
    except IntegrityError:
        # A concurrent retry may have won the creator/event unique key.
        await db.rollback()
        existing = await _existing_execution(
            db, creator_id=creator_id, client_event_id=body.client_event_id
        )
        if (
            existing is None
            or existing.proposal_receipt_id != proposal_uuid
            or not _execution_matches(existing, body)
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="client_event_id was already used for a different receipt",
            )
        return _idempotent_response(proposal_uuid, existing, recorded=False)
    return _idempotent_response(proposal_uuid, execution, recorded=True)


async def stage_copilot_save_links(
    db: AsyncSession,
    *,
    proposal_ids: list[uuid.UUID],
    creator_id: uuid.UUID,
    plan_item_id: uuid.UUID,
    job_id: uuid.UUID,
    variant_id: str,
    revision_hash: str,
) -> int:
    """Append conservative links from applied Copilot turns to one atomic Save.

    The browser sends only the latest turn whose undo token still equals the
    editor's live history version. We additionally require its execution event
    to have arrived with ``applied``. Missing/raced audit evidence is skipped;
    Save correctness never depends on training telemetry.
    """
    if not proposal_ids or not revision_hash:
        return 0
    proposals = (
        (
            await db.execute(
                select(EditInteractionReceipt).where(
                    EditInteractionReceipt.id.in_(proposal_ids),
                    EditInteractionReceipt.event_kind == "proposal",
                    EditInteractionReceipt.creator_id == creator_id,
                    EditInteractionReceipt.plan_item_id == plan_item_id,
                    EditInteractionReceipt.job_id == job_id,
                    EditInteractionReceipt.variant_id == variant_id,
                    EditInteractionReceipt.proposal_outcome == "applied",
                )
            )
        )
        .scalars()
        .all()
    )
    staged = 0
    for proposal in proposals:
        execution = (
            await db.execute(
                select(EditInteractionReceipt)
                .where(
                    EditInteractionReceipt.proposal_receipt_id == proposal.id,
                    EditInteractionReceipt.event_kind == "execution",
                    EditInteractionReceipt.execution_outcome == "applied",
                )
                .order_by(EditInteractionReceipt.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if execution is None:
            continue
        client_event_id = f"save:{revision_hash}:{proposal.id}"
        existing = (
            await db.execute(
                select(EditInteractionReceipt.id).where(
                    EditInteractionReceipt.creator_id == creator_id,
                    EditInteractionReceipt.client_event_id == client_event_id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            continue
        await _add(
            db,
            EditInteractionReceipt(
                id=uuid.uuid4(),
                event_kind="save_link",
                proposal_receipt_id=proposal.id,
                creator_id=creator_id,
                plan_item_id=plan_item_id,
                job_id=job_id,
                variant_id=variant_id,
                client_event_id=client_event_id,
                utterance=proposal.utterance,
                inferred_intent=proposal.inferred_intent,
                model_reply=proposal.model_reply,
                eligibility_basis=proposal.eligibility_basis,
                consent_event_id=proposal.consent_event_id,
                internal_grant_id=proposal.internal_grant_id,
                proposed_operations=list(proposal.proposed_operations or []),
                proposed_operations_digest=proposal.proposed_operations_digest,
                prompt_version=proposal.prompt_version,
                model=proposal.model,
                proposal_outcome=proposal.proposal_outcome,
                execution_outcome="applied",
                rejection_reasons=[],
                before_revision_hash=execution.before_revision_hash,
                after_revision_hash=revision_hash,
            ),
        )
        staged += 1
    return staged
