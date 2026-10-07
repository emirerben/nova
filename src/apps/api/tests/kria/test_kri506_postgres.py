"""Real event/lease/approval boundaries for creative-copy consent."""

from __future__ import annotations

import asyncio
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.agents._schemas.creator_agent import CreativeStrategy, ProposeStrategy
from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria.api_schemas import ApprovalDecisionBody, SubmitTurnBody
from app.kria.brief_binding import snapshot_media
from app.kria.planner import PlannedKriaTurn, adapt_creator_action
from app.kria.runtime import RuntimeFailure, decide_approval, submit_turn
from app.models import (
    CreationThread,
    CreationThreadEvent,
    CreatorAgentApproval,
    CreatorAgentSession,
    CreatorEditDraft,
    PlanItem,
)
from app.services.creative_copy_decisions import media_digest, wording_question
from app.tasks.kria_runtime import _append_sync_event, run_kria_turn
from tests.kria.test_runtime_postgres_integration import (
    _await_montage_approval,
    _seed_runtime_project,
)


@pytest_asyncio.fixture(autouse=True)
async def _flags_and_pool(monkeypatch):
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "kria_choice_questions_enabled", True)
    yield
    await async_engine.dispose()


def _ask(thread_id, session_id, candidate="A day worth remembering", *, history=0):
    with sync_session() as db:
        item = db.get(PlanItem, db.get(CreatorAgentSession, session_id).plan_item_id)
        question = wording_question(
            target="opening_title",
            candidate=candidate,
            dependency_digest=media_digest(snapshot_media(item)),
        )
        thread = db.get(CreationThread, thread_id, with_for_update=True)
        _append_sync_event(
            db,
            thread,
            role="assistant",
            event_type="assistant_response",
            content=f"“{candidate}” — does this wording work?",
            payload={"choice_question": question, "turn_value": "question"},
        )
        for _ in range(history):
            _append_sync_event(
                db,
                thread,
                role="user",
                event_type="user_message",
                content="Let's discuss pacing.",
                payload={},
            )
            _append_sync_event(
                db,
                thread,
                role="assistant",
                event_type="assistant_response",
                content="We can keep it relaxed.",
                payload={},
            )
        db.commit()
        return question


async def _submit(user_id, thread_id, message, **kwargs):
    with sync_session() as db:
        revision = db.get(CreationThread, thread_id).revision
    async with AsyncSessionLocal() as db:
        return await submit_turn(
            db,
            thread_id=thread_id,
            creator_id=user_id,
            body=SubmitTurnBody(
                message=message,
                client_event_id=f"copy-{uuid.uuid4().hex}",
                expected_thread_revision=revision,
                **kwargs,
            ),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [True, False])
async def test_typed_answer_survives_long_discussion_and_flag_change(monkeypatch, flag):
    user_id, thread_id, session_id = _seed_runtime_project()
    question = _ask(thread_id, session_id, history=65)
    monkeypatch.setattr(settings, "kria_choice_questions_enabled", flag)
    accepted, _ = await _submit(user_id, thread_id, "Use this wording")
    with sync_session() as db:
        event = (
            db.execute(
                select(CreationThreadEvent)
                .where(CreationThreadEvent.thread_id == thread_id)
                .order_by(CreationThreadEvent.sequence.desc())
            )
            .scalars()
            .first()
        )
        assert event.payload["choice_selection"] == {
            "question_id": question["question_id"],
            "option_key": "approve",
        }
    assert accepted.status == "pending"


@pytest.mark.asyncio
async def test_fake_or_superseded_question_id_is_rejected():
    user_id, thread_id, session_id = _seed_runtime_project()
    old = _ask(thread_id, session_id)
    _ask(thread_id, session_id, candidate="A revised candidate")
    with pytest.raises(RuntimeFailure, match="no longer open"):
        await _submit(
            user_id,
            thread_id,
            "Use this wording",
            choice_selection={"question_id": old["question_id"], "option_key": "approve"},
        )


def _plan(title):
    return PlannedKriaTurn(
        plan=adapt_creator_action(
            ProposeStrategy(
                kind="propose_strategy",
                strategy=CreativeStrategy(
                    direction="guided_story",
                    edit_format="day_vlog",
                    audio_strategy="licensed_music",
                    render_program="guided",
                    pacing="fast",
                    selected_media_ids=[],
                    opening_title=title,
                    rationale="Use the approved opening before the diary.",
                ),
                summary="Open with your chosen wording.",
            )
        ),
        manifest_hash="a" * 64,
        context_hash="b" * 64,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer,expected", [("Render now", "completed"), ("Use this wording", "awaiting_approval")]
)
async def test_wording_approval_is_separate_from_render_approval(monkeypatch, answer, expected):
    user_id, thread_id, session_id = _seed_runtime_project()
    candidate = "A day worth remembering"
    _ask(thread_id, session_id, candidate)

    async def planned(*_args, **_kwargs):
        return _plan(candidate)

    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", planned)
    accepted, _ = await _submit(user_id, thread_id, answer)
    result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
    assert result["status"] == expected
    with sync_session() as db:
        approvals = list(
            db.execute(
                select(CreatorAgentApproval).where(CreatorAgentApproval.thread_id == thread_id)
            ).scalars()
        )
        if answer == "Render now":
            assert not approvals
        else:
            assert len(approvals) == 1 and approvals[0].status == "pending"
            draft = db.get(CreatorEditDraft, approvals[0].draft_id)
            assert draft.snapshot_json["strategy"]["opening_title"] == candidate


@pytest.mark.asyncio
async def test_pending_copy_invalidates_previously_created_render_approval(monkeypatch):
    (
        user,
        thread,
        _item,
        approval,
        fingerprint,
        _revision,
        draft_revision,
    ) = await _await_montage_approval(monkeypatch, suffix="kri506-pending")
    with sync_session() as db:
        session_id = db.get(CreationThread, thread).active_creator_agent_session_id
    _ask(thread, session_id)
    with sync_session() as db:
        revision = db.get(CreationThread, thread).revision
    with pytest.raises(RuntimeFailure) as error:
        async with AsyncSessionLocal() as db:
            await decide_approval(
                db,
                thread_id=thread,
                approval_id=approval,
                creator_id=user,
                decision="approve",
                body=ApprovalDecisionBody(
                    expected_thread_revision=revision,
                    expected_draft_revision=draft_revision,
                    expected_approval_fingerprint=fingerprint,
                ),
            )
    assert error.value.code == "creative_copy_pending"
    with sync_session() as db:
        assert db.get(CreatorAgentApproval, approval).status == "cancelled"
