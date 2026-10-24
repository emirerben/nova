"""KRI-898: the app build is stamped where the creator acts, and reaches the render.

The freeze hold is planned only for an app build that can play it
(`tests/kria/test_kri898_freeze_old_client.py`). That only works if the build reaches the
Celery side intact, so this crosses the real HTTP-service / Celery boundary on Postgres:

1. Submit records the sending build on the message; the worker's claim snapshot carries it to
   the planner (the draft's freeze question and draft check read it).
2. An idempotent replay of the same ``client_event_id`` from another build is still the same
   request (the stamp is not part of ``request_digest``).
3. Approve records the APPROVING build, and dispatch hands it to the Job
   (``creator_client_protocol``); an approval with no build header hands nothing (unstamped,
   so the render plans no hold).
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select

from app.agents._schemas.creator_agent import CreativeStrategy, ProposeStrategy
from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria.api_schemas import ApprovalDecisionBody, SubmitTurnBody
from app.kria.planner import PlannedKriaTurn, adapt_creator_action
from app.kria.runtime import approval_fingerprint, decide_approval, submit_turn
from app.models import (
    CreationThread,
    CreationThreadEvent,
    CreatorAgentApproval,
    CreatorAgentSession,
    CreatorAgentTurn,
    Job,
    PlanItem,
)
from app.services.client_protocol import clear_request_context, set_client_protocol
from app.tasks.content_plan_build import DispatchResult
from app.tasks.kria_runtime import execute_kria_approval, run_kria_turn
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project


@pytest.fixture(autouse=True)
def _runtime_enabled(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN202
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    yield
    clear_request_context()


def _strategy_plan():  # noqa: ANN202
    return adapt_creator_action(
        ProposeStrategy(
            kind="propose_strategy",
            strategy=CreativeStrategy(
                direction="guided_story",
                edit_format="day_vlog",
                audio_strategy="licensed_music",
                pacing="fast",
                render_program="guided",
                selected_media_ids=[],
                rationale="Open on the whisk and end on the packed order.",
            ),
            summary="Open on the whisk and finish on the packed order.",
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("approved_on", [15, None], ids=["approved-on-15", "approved-headerless"])
async def test_the_sending_and_approving_builds_reach_the_planner_and_the_job(
    monkeypatch: pytest.MonkeyPatch, approved_on: int | None
) -> None:
    user_id, thread_id, session_id = _seed_runtime_project()
    body = SubmitTurnBody(
        message="Freeze the frame for one second on the whisk",
        client_event_id=f"kri898-{uuid.uuid4().hex}",
        expected_thread_revision=2,
    )
    planner_snapshots: list[dict] = []

    async def _planned(snapshot, *_args, **_kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        planner_snapshots.append(dict(snapshot))
        return PlannedKriaTurn(plan=_strategy_plan(), manifest_hash="a" * 64, context_hash="b" * 64)

    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", _planned)
    dispatched_with: list[dict] = []
    try:
        # 1. the message is sent from a protocol-16 build
        set_client_protocol(16)
        async with AsyncSessionLocal() as db:
            accepted, _ = await submit_turn(db, thread_id=thread_id, creator_id=user_id, body=body)
        # 2. the same message re-sent from a protocol-15 build is the same request
        set_client_protocol(15)
        async with AsyncSessionLocal() as db:
            replayed, _ = await submit_turn(db, thread_id=thread_id, creator_id=user_id, body=body)
        assert replayed.turn_id == accepted.turn_id
        with sync_session() as db:
            turn = db.get(CreatorAgentTurn, uuid.UUID(accepted.turn_id))
            source = db.get(CreationThreadEvent, turn.source_event_id)
            # the FIRST stored stamp wins, like the rest of the message
            assert source.payload["client_protocol"] == 16

        # The worker runs with no request context (Celery).
        clear_request_context()
        result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
        assert result == {"turn_id": accepted.turn_id, "status": "awaiting_approval"}
        assert planner_snapshots and planner_snapshots[-1]["client_protocol"] == 16

        with sync_session() as db:
            approval = db.execute(
                select(CreatorAgentApproval).where(
                    CreatorAgentApproval.turn_id == uuid.UUID(accepted.turn_id)
                )
            ).scalar_one()
            approval_id = approval.id
            token = approval_fingerprint(approval)
            revision = db.get(CreationThread, thread_id).revision
            item_id = db.get(CreatorAgentSession, session_id).plan_item_id
            draft_revision = approval.draft_revision

        # 3. approve from the approving build (or from a caller that sends no build)
        if approved_on is not None:
            set_client_protocol(approved_on)
        else:
            set_client_protocol(None)
        async with AsyncSessionLocal() as db:
            decision, _ = await decide_approval(
                db,
                thread_id=thread_id,
                approval_id=approval_id,
                creator_id=user_id,
                decision="approve",
                body=ApprovalDecisionBody(
                    expected_thread_revision=revision,
                    expected_draft_revision=draft_revision,
                    expected_approval_fingerprint=token,
                ),
            )
        assert decision.status == "approved"
        clear_request_context()

        def _dispatch(*_args, **kwargs) -> DispatchResult:  # noqa: ANN002, ANN003
            dispatched_with.append(kwargs)
            with sync_session() as db:
                item = db.get(PlanItem, item_id, with_for_update=True)
                job = Job(
                    user_id=user_id,
                    status="queued",
                    mode="generative",
                    raw_storage_path="",
                    selected_platforms=["tiktok"],
                    content_plan_item_id=item.id,
                    content_plan_ownership_epoch=0,
                    assembly_plan={"variants": []},
                )
                db.add(job)
                db.flush()
                item.current_job_id = job.id
                db.commit()
                return DispatchResult("dispatched", job_id=str(job.id))

        monkeypatch.setattr("app.tasks.content_plan_build.dispatch_item_render_for", _dispatch)
        dispatched = await asyncio.to_thread(execute_kria_approval.run, str(approval_id))
        assert dispatched["status"] == "dispatched"
    finally:
        clear_request_context()
        await async_engine.dispose()

    (kwargs,) = dispatched_with
    if approved_on is None:
        # unstamped: the Job gets no build, so the render plans no freeze hold
        assert "creator_client_protocol" not in kwargs
    else:
        # the APPROVING build, not the one that drafted (16)
        assert kwargs["creator_client_protocol"] == approved_on
