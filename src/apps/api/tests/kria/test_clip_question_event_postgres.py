"""KRI-282: the question event carries `clip_question` only when the plan has one."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria.api_schemas import SubmitTurnBody
from app.kria.contracts import KriaTurnPlan
from app.kria.planner import PlannedKriaTurn
from app.kria.runtime import submit_turn
from app.models import CreationThread, CreationThreadEvent
from app.tasks.kria_runtime import run_kria_turn
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

CQ = {
    "version": 1,
    "question_id": str(uuid.uuid4()),
    "allow_none": True,
    "categories": [
        {
            "key": "group:dodgeball",
            "label": "Dodgeball",
            "op": "group",
            "candidate_media_ids": ["m1"],
            "suggested_media_ids": [],
        }
    ],
}


@pytest.mark.asyncio
@pytest.mark.parametrize("with_question", [True, False])
async def test_question_event_carries_clip_question_only_when_planned(
    monkeypatch, with_question: bool
) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)

    async def _planned(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        return PlannedKriaTurn(
            plan=KriaTurnPlan(
                mode="respond",
                turn_value="question",
                response="Tap the clips that show dodgeball.",
                clip_question=CQ if with_question else None,
            ),
            manifest_hash="a" * 64,
            context_hash="b" * 64,
        )

    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", _planned)
    user_id, thread_id, _ = _seed_runtime_project()
    try:
        with sync_session() as db:
            revision = db.get(CreationThread, thread_id).revision
        async with AsyncSessionLocal() as db:
            accepted, _ = await submit_turn(
                db,
                thread_id=thread_id,
                creator_id=user_id,
                body=SubmitTurnBody(
                    message="Group the dodgeball clips",
                    client_event_id=f"q-{uuid.uuid4().hex}",
                    expected_thread_revision=revision,
                ),
            )
        await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
        with sync_session() as db:
            payloads = [
                p
                for p in db.execute(
                    select(CreationThreadEvent.payload).where(
                        CreationThreadEvent.thread_id == thread_id,
                        CreationThreadEvent.event_type == "assistant_response",
                    )
                )
                .scalars()
                .all()
                if p and p.get("turn_value") == "question"
            ]
        assert len(payloads) == 1
        if with_question:
            assert payloads[0]["clip_question"] == CQ
        else:
            assert "clip_question" not in payloads[0]
    finally:
        await async_engine.dispose()
