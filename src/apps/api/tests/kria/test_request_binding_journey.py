"""KRI-459: an approval dispatches the request it drafted against."""

from __future__ import annotations

import asyncio
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria.api_schemas import ApprovalDecisionBody
from app.kria.runtime import approval_fingerprint, decide_approval
from app.models import (
    CreationThread,
    CreativeBriefVersion,
    CreatorAgentApproval,
    CreatorAgentSession,
    PlanItem,
)
from app.tasks.kria_runtime import _claim_approval_dispatch, run_kria_turn
from tests.kria.test_runtime_postgres_integration import (
    _brief_strategy_plan,
    _brief_updates,
    _seed_runtime_project,
    _submit,
)


@pytest.fixture(autouse=True)
def _binding_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", True)
    monkeypatch.setattr(settings, "kria_creative_brief_user_ids", [])
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)
    monkeypatch.setattr(settings, "kria_brief_binding_user_ids", [])


@pytest_asyncio.fixture(autouse=True)
async def _dispose_async_engine():
    yield
    await async_engine.dispose()


async def _approved_turn(monkeypatch: pytest.MonkeyPatch) -> tuple[uuid.UUID, uuid.UUID]:
    user_id, thread_id, session_id = _seed_runtime_project()
    with sync_session() as db:
        session = db.get(CreatorAgentSession, session_id)
        assert session is not None
        item = db.get(PlanItem, session.plan_item_id)
        assert item is not None
        item.clip_gcs_paths = ["clips/one.mp4"]
        item.clip_assignments = [
            {
                "media_id": "clip-1",
                "gcs_path": "clips/one.mp4",
                "storage_generation": "g1",
            }
        ]
        db.commit()

    async def _planned(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        from app.kria.planner import PlannedKriaTurn

        return PlannedKriaTurn(
            plan=_brief_strategy_plan(),
            manifest_hash="a" * 64,
            context_hash="b" * 64,
            brief_updates=_brief_updates(),
            brief_route="replan",
            brief_clip_ids=("clip-1",),
        )

    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", _planned)
    accepted = await _submit(
        user_id,
        thread_id,
        "Title it 20K Kosu and label each clip with its landmark",
        2,
    )
    result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
    assert result["status"] == "awaiting_approval"

    with sync_session() as db:
        approval = db.execute(
            select(CreatorAgentApproval).where(
                CreatorAgentApproval.turn_id == uuid.UUID(accepted.turn_id)
            )
        ).scalar_one()
        thread = db.get(CreationThread, thread_id)
        assert thread is not None
        approval_id = approval.id
        expected_revision = thread.revision
        expected_draft_revision = approval.draft_revision
        token = approval_fingerprint(approval)

    async with AsyncSessionLocal() as db:
        decision, _ = await decide_approval(
            db,
            thread_id=thread_id,
            approval_id=approval_id,
            creator_id=user_id,
            decision="approve",
            body=ApprovalDecisionBody(
                expected_thread_revision=expected_revision,
                expected_draft_revision=expected_draft_revision,
                expected_approval_fingerprint=token,
            ),
        )
    assert decision.status == "approved"
    return approval_id, thread_id


@pytest.mark.asyncio
@pytest.mark.parametrize("disable_flag_after_draft", [False, True])
async def test_approval_dispatch_uses_pinned_request_after_later_brief(
    monkeypatch: pytest.MonkeyPatch, disable_flag_after_draft: bool
) -> None:
    approval_id, thread_id = await _approved_turn(monkeypatch)

    # A later brief version supersedes the live ledger but must not rewrite the
    # binding already embedded in the approved draft.
    with sync_session() as db:
        db.add(
            CreativeBriefVersion(
                thread_id=thread_id,
                version=2,
                requirements=[
                    {
                        "id": "later",
                        "kind": "text",
                        "scope": "title",
                        "literal": "Later title",
                        "description": None,
                        "facts": {},
                        "status": "open",
                        "source_turn_id": None,
                    }
                ],
                source_turn_id=None,
            )
        )
        db.commit()
    if disable_flag_after_draft:
        monkeypatch.setattr(settings, "kria_brief_binding_enabled", False)

    claim = await asyncio.to_thread(_claim_approval_dispatch, approval_id)
    assert claim is not None
    assert claim.brief_binding is not None
    assert '"20K Kosu"' in claim.creator_request
    assert "Later title" not in claim.creator_request


@pytest.mark.asyncio
async def test_media_generation_change_invalidates_pinned_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approval_id, thread_id = await _approved_turn(monkeypatch)
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        assert thread is not None
        session = db.get(CreatorAgentSession, thread.active_creator_agent_session_id)
        assert session is not None
        item = db.get(PlanItem, session.plan_item_id)
        assert item is not None
        item.clip_assignments = [
            {"media_id": "clip-1", "gcs_path": "clips/one.mp4", "storage_generation": "g2"}
        ]
        db.commit()

    assert await asyncio.to_thread(_claim_approval_dispatch, approval_id) is None
