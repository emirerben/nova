"""Reader persistence boundary, not an original-incident journey replay."""

import asyncio
import uuid

import pytest
from sqlalchemy import select

from app.agents._schemas.creator_agent import CreativeStrategy, ProposeStrategy
from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria.api_schemas import SubmitTurnBody
from app.kria.planner import PlannedKriaTurn, adapt_creator_action
from app.kria.runtime import submit_turn
from app.models import CreatorAgentApproval, CreatorEditDraft
from app.schemas.clip_intents import ClipIntent
from app.schemas.story_intents import RejectedClipSelection
from app.services.clip_selection import intent_key
from app.tasks.kria_runtime import run_kria_turn
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project


@pytest.mark.asyncio
async def test_reconciliation_reader_survives_real_draft_writer_and_pinned_approval(monkeypatch):
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    user_id, thread_id, _ = _seed_runtime_project()
    rejected = ClipIntent(intent_id="inside_cabildo", op="order", attribute="Cabildo interior")
    story = ["Show the interior", "Then the palace"]
    plan = adapt_creator_action(
        ProposeStrategy(
            kind="propose_strategy",
            summary="All five scenes will appear.",
            strategy=CreativeStrategy(
                direction="guided_story",
                edit_format="day_vlog",
                audio_strategy="licensed_music",
                render_program="guided",
                selected_media_ids=[],
                story_structure=story,
            ),
        ),
        server_clip_intents=[],
        server_resolved_clip_intents=[],
        server_checked_intents=[rejected],
        server_rejected_selections=[
            RejectedClipSelection(rejected, "checked", intent_key(rejected), 1)
        ],
    )

    async def prepared_boundary(*args, **kwargs):
        return PlannedKriaTurn(plan=plan, manifest_hash="a" * 64, context_hash="b" * 64)

    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", prepared_boundary)
    try:
        async with AsyncSessionLocal() as db:
            accepted, _ = await submit_turn(
                db,
                thread_id=thread_id,
                creator_id=user_id,
                body=SubmitTurnBody(
                    message="Leave out the interior",
                    client_event_id=f"reader-{uuid.uuid4().hex}",
                    expected_thread_revision=2,
                ),
            )
        result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
        assert result["status"] == "awaiting_approval", result
        with sync_session() as db:
            approval = db.execute(
                select(CreatorAgentApproval).where(
                    CreatorAgentApproval.turn_id == uuid.UUID(accepted.turn_id)
                )
            ).scalar_one()
            draft = db.get(CreatorEditDraft, approval.draft_id)
            saved = CreativeStrategy.model_validate(draft.snapshot_json["strategy"])
            assert approval.status == "pending"
            assert saved.story_structure == []
            assert saved.selection_reconciliation.unresolved_story_rows == tuple(story)
            assert saved.selection_reconciliation.rejected_selections[0].question_id == "checked"
            assert "All five scenes will appear." not in str(draft.snapshot_json)
    finally:
        await async_engine.dispose()
