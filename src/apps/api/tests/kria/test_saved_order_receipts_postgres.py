"""Old stored receipts cannot promote an unrecorded internal order to checked success."""

import copy
import uuid

import pytest

from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria.drafts import KriaDraftDocument, canonical_snapshot
from app.kria.runtime import read_creative_brief
from app.models import CreationThread, CreationThreadEvent, CreativeBriefVersion, CreatorEditDraft
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project
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
    requirement = copy.deepcopy(next(r for r in FIXTURE["brief"]["requirements"] if r["id"] == "r2"))
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
