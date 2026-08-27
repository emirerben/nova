from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.models import EditInteractionReceipt
from app.routes._copilot import CopilotTurnBody, CopilotTurnResponse
from app.services.edit_interaction_receipts import (
    ExecuteCopilotReceiptBody,
    persist_copilot_execution,
    persist_copilot_proposal,
    stage_copilot_save_links,
)


def _result(value):  # noqa: ANN001
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result


def _rows(values):  # noqa: ANN001
    result = MagicMock()
    result.scalars.return_value.all.return_value = values
    return result


def _proposal(**overrides) -> EditInteractionReceipt:  # noqa: ANN003
    values = {
        "id": uuid.uuid4(),
        "event_kind": "proposal",
        "proposal_receipt_id": None,
        "creator_id": uuid.uuid4(),
        "plan_item_id": uuid.uuid4(),
        "job_id": uuid.uuid4(),
        "variant_id": "v1",
        "client_event_id": None,
        "utterance": "Make it smaller",
        "inferred_intent": "edit",
        "model_reply": "Done",
        "eligibility_basis": "training_consent",
        "consent_event_id": uuid.uuid4(),
        "internal_grant_id": None,
        "proposed_operations": [{"op": "set_text_size", "bar_index": 0, "size_px": 48}],
        "proposed_operations_digest": "d" * 64,
        "prompt_version": "prompt-v1",
        "model": "model-v1",
        "proposal_outcome": "proposed",
        "execution_outcome": None,
        "rejection_reasons": [],
        "before_revision_hash": "before",
        "after_revision_hash": None,
    }
    values.update(overrides)
    return EditInteractionReceipt(**values)


@pytest.mark.asyncio
async def test_proposal_receipt_records_exact_final_ops() -> None:
    db = MagicMock()
    db.commit = AsyncMock()
    creator_id, item_id, job_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    response = CopilotTurnResponse(
        intent="edit",
        ops=[{"op": "set_text_size", "bar_index": 0, "size_px": 48}],
        confidence=0.9,
        reply="Done",
        outcome="proposed",
    )
    body = CopilotTurnBody(
        message="x" * 2000,
        snapshot={
            "guided_revision": {
                "revision_number": 2,
                "base_generation": "render-2",
                "state_hash": "state-2",
            }
        },
    )
    consent_id = uuid.uuid4()

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            "app.services.edit_interaction_receipts.evaluate_training_eligibility_async",
            AsyncMock(
                return_value=MagicMock(
                    eligible=True,
                    basis="training_consent",
                    consent_event_id=consent_id,
                    internal_grant_id=None,
                )
            ),
        )

        recorded = await persist_copilot_proposal(
            db,
            creator_id=creator_id,
            plan_item_id=item_id,
            job_id=job_id,
            variant_id="v1",
            body=body,
            response=response,
        )

    row = db.add.call_args.args[0]
    assert recorded.receipt_id == str(row.id)
    assert row.proposed_operations == response.ops
    assert row.model_reply == "Done"
    assert row.utterance == "x" * 2000
    assert row.consent_event_id == consent_id
    assert row.before_revision_hash == "state-2"
    assert len(row.proposed_operations_digest) == 64
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_proposal_receipt_is_not_retained_without_training_eligibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = MagicMock()
    db.commit = AsyncMock()
    monkeypatch.setattr(
        "app.services.edit_interaction_receipts.evaluate_training_eligibility_async",
        AsyncMock(return_value=MagicMock(eligible=False)),
    )
    response = CopilotTurnResponse(
        intent="edit",
        ops=[{"op": "remove_text", "bar_index": 0}],
        confidence=0.9,
        reply="Removed it.",
        outcome="proposed",
    )

    returned = await persist_copilot_proposal(
        db,
        creator_id=uuid.uuid4(),
        plan_item_id=uuid.uuid4(),
        job_id=uuid.uuid4(),
        variant_id="v1",
        body=CopilotTurnBody(message="Remove it", snapshot={}),
        response=response,
    )

    assert returned is response
    assert returned.receipt_id is None
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_execution_retry_returns_original_append_only_row() -> None:
    proposal = _proposal()
    body = ExecuteCopilotReceiptBody(
        client_event_id="stable-event",
        outcome="rejected",
        rejection_reasons=[{"op": "set_text_size", "reason": "stale", "detail": "text changed"}],
        before_revision_hash="before",
        after_revision_hash="before",
    )
    first_db = MagicMock()
    first_db.execute = AsyncMock(side_effect=[_result(proposal), _result(None)])
    first_db.commit = AsyncMock()
    first_db.rollback = AsyncMock()

    first = await persist_copilot_execution(
        first_db,
        proposal_id=proposal.id,
        creator_id=proposal.creator_id,
        plan_item_id=proposal.plan_item_id,
        variant_id=proposal.variant_id,
        body=body,
    )
    execution = first_db.add.call_args.args[0]
    assert first.recorded is True
    assert execution.proposal_receipt_id == proposal.id
    assert execution.proposed_operations == proposal.proposed_operations
    assert execution.model_reply == proposal.model_reply

    retry_db = MagicMock()
    retry_db.execute = AsyncMock(side_effect=[_result(proposal), _result(execution)])
    retry_db.commit = AsyncMock()
    retry = await persist_copilot_execution(
        retry_db,
        proposal_id=proposal.id,
        creator_id=proposal.creator_id,
        plan_item_id=proposal.plan_item_id,
        variant_id=proposal.variant_id,
        body=body,
    )
    assert retry.recorded is False
    assert retry.execution_receipt_id == str(execution.id)
    retry_db.add.assert_not_called()
    retry_db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_client_event_cannot_be_reused_for_another_proposal() -> None:
    proposal = _proposal()
    other_execution = _proposal(
        id=uuid.uuid4(),
        event_kind="execution",
        proposal_receipt_id=uuid.uuid4(),
        client_event_id="stable-event",
        execution_outcome="applied",
    )
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[_result(proposal), _result(other_execution)])

    with pytest.raises(HTTPException) as exc:
        await persist_copilot_execution(
            db,
            proposal_id=proposal.id,
            creator_id=proposal.creator_id,
            plan_item_id=proposal.plan_item_id,
            variant_id=proposal.variant_id,
            body=ExecuteCopilotReceiptBody(
                client_event_id="stable-event",
                outcome="applied",
                rejection_reasons=[],
                before_revision_hash="before",
                after_revision_hash=None,
            ),
        )
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_save_link_requires_staged_execution_and_uses_canonical_revision() -> None:
    proposal = _proposal()
    execution = _proposal(
        id=uuid.uuid4(),
        event_kind="execution",
        proposal_receipt_id=proposal.id,
        client_event_id="client-event",
        execution_outcome="staged",
    )
    db = MagicMock()
    db.execute = AsyncMock(
        side_effect=[
            _rows([proposal]),
            _result(execution),
            _result(None),
        ]
    )

    staged = await stage_copilot_save_links(
        db,
        proposal_ids=[proposal.id],
        creator_id=proposal.creator_id,
        plan_item_id=proposal.plan_item_id,
        job_id=proposal.job_id,
        variant_id=proposal.variant_id,
        revision_hash="canonical-saved-state",
    )

    assert staged == 1
    link = db.add.call_args.args[0]
    assert link.event_kind == "save_link"
    assert link.proposal_receipt_id == proposal.id
    assert link.after_revision_hash == "canonical-saved-state"
    assert link.execution_outcome == "applied"
    assert link.model_reply == proposal.model_reply


@pytest.mark.asyncio
async def test_save_link_skips_when_execution_receipt_has_not_arrived() -> None:
    proposal = _proposal()
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[_rows([proposal]), _result(None)])

    staged = await stage_copilot_save_links(
        db,
        proposal_ids=[proposal.id],
        creator_id=proposal.creator_id,
        plan_item_id=proposal.plan_item_id,
        job_id=proposal.job_id,
        variant_id=proposal.variant_id,
        revision_hash="canonical-saved-state",
    )

    assert staged == 0
    db.add.assert_not_called()
