"""Old stored receipts cannot promote an unrecorded internal order to checked success."""

import asyncio
import copy
import uuid

import pytest

from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria.api_schemas import ApprovalDecisionBody
from app.kria.approval_readiness import NOT_READY
from app.kria.brief import BriefUpdate
from app.kria.drafts import KriaDraftDocument, canonical_snapshot
from app.kria.planner import PlannedKriaTurn
from app.kria.runtime import (
    RuntimeFailure,
    approval_fingerprint,
    decide_approval,
    read_creative_brief,
)
from app.models import (
    CreationThread,
    CreationThreadEvent,
    CreativeBriefVersion,
    CreatorAgentApproval,
    CreatorEditDraft,
)
from app.tasks.kria_runtime import run_kria_turn
from tests.kria.test_runtime_postgres_integration import (
    _brief_strategy_plan,
    _seed_runtime_project,
    _submit,
)
from tests.pipeline.test_kri941_listed_order_replay import FIXTURE, _recorded_strategy


@pytest.mark.parametrize("explicit_group", [False, True], ids=["legacy-list", "confirmed-group"])
async def test_reopening_a_stored_order_receipt_preserves_its_actual_certainty(
    monkeypatch: pytest.MonkeyPatch, explicit_group: bool
) -> None:
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", True)
    user_id, thread_id, _ = _seed_runtime_project()
    strategy = _recorded_strategy()
    if explicit_group:
        for intent in strategy["resolved_clip_intents"]:
            if intent["intent_id"] == "order_sequence_between":
                intent["ordered_members"] = False
    snapshot, snapshot_hash = canonical_snapshot(
        KriaDraftDocument(kind="strategy", strategy=strategy)
    )
    draft_id = uuid.uuid4()
    requirement = copy.deepcopy(
        next(r for r in FIXTURE["brief"]["requirements"] if r["id"] == "r2")
    )
    historical = {
        "requirement_id": "r2",
        "status": "met",
        "verification": "checked",
        "stage": "applied",
        "brief_version": 1,
    }
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        db.add(CreativeBriefVersion(thread_id=thread_id, version=1, requirements=[requirement]))
        db.add(
            CreatorEditDraft(
                id=draft_id,
                creator_id=user_id,
                thread_id=thread_id,
                item_id=thread.active_plan_item_id,
                variant_key="v1",
                draft_revision=1,
                snapshot_json=snapshot,
                snapshot_hash=snapshot_hash,
                is_head=True,
            )
        )
        thread.revision = 3
        db.add(
            CreationThreadEvent(
                thread_id=thread_id,
                sequence=2,
                revision=3,
                role="assistant",
                event_type="draft_applied",
                payload={"draft_id": str(draft_id), "requirement_receipts": [historical]},
            )
        )
        db.commit()
    try:
        async with AsyncSessionLocal() as db:
            shown = await read_creative_brief(db, thread_id=thread_id, creator_id=user_id)
        (receipt,) = shown.requirement_receipts
        assert receipt.status == ("met" if explicit_group else "partial")
        assert shown.requirements[0].status == receipt.status
        if not explicit_group:
            assert "saved" in receipt.reason.casefold()
        with sync_session() as db:
            stored = db.get(CreatorEditDraft, draft_id)
            assert (stored.snapshot_json, stored.snapshot_hash) == (snapshot, snapshot_hash)
            event = db.query(CreationThreadEvent).filter_by(thread_id=thread_id, sequence=2).one()
            assert event.payload["requirement_receipts"] == [historical]
    finally:
        await async_engine.dispose()


async def test_a_cached_approval_without_readiness_cannot_approve_unconfirmed_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, thread_id, _ = _seed_runtime_project()
    plan = _brief_strategy_plan()
    plan.intents[0].arguments["strategy"]["resolved_clip_intents"] = [
        {
            "intent_id": "middle",
            "op": "order",
            "attribute": "the two middle clips",
            "status": "resolved",
            "assignments": [
                {"media_id": "clip-1", "confidence": 1, "evidence": "creator selected it"},
                {"media_id": "clip-2", "confidence": 1, "evidence": "creator selected it"},
            ],
        }
    ]

    async def planned(*_args, **_kwargs):
        return PlannedKriaTurn(
            plan=plan,
            manifest_hash="a" * 64,
            context_hash="b" * 64,
            brief_updates=(
                BriefUpdate(kind="order", scope="global", description="the listed order"),
            ),
            brief_route="replan",
            brief_clip_ids=("clip-1", "clip-2"),
        )

    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", True)
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", False)
    monkeypatch.setattr(settings, "kria_approval_readiness_enabled", False)
    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", planned)
    try:
        accepted = await _submit(user_id, thread_id, "Keep the listed order.", 2)
        result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
        assert result["status"] == "awaiting_approval"
        with sync_session() as db:
            approval = (
                db.query(CreatorAgentApproval).filter_by(turn_id=uuid.UUID(accepted.turn_id)).one()
            )
            approval_id = approval.id
            body = ApprovalDecisionBody(
                expected_thread_revision=db.get(CreationThread, thread_id).revision,
                expected_draft_revision=approval.draft_revision,
                expected_approval_fingerprint=approval_fingerprint(approval),
            )
        monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)
        monkeypatch.setattr(settings, "kria_approval_readiness_enabled", True)
        with pytest.raises(RuntimeFailure) as failure:
            async with AsyncSessionLocal() as db:
                await decide_approval(
                    db,
                    thread_id=thread_id,
                    approval_id=approval_id,
                    creator_id=user_id,
                    decision="approve",
                    body=body,
                )
        assert failure.value.code == NOT_READY
        with sync_session() as db:
            assert db.get(CreatorAgentApproval, approval_id).status == "cancelled"
    finally:
        await async_engine.dispose()
