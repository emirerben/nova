"""KRI-470: after the render-time title block the creator's reply is honoured deterministically.

Real runtime path on the scratch Postgres: a failed unified-montage job (real receipts from the
real planner, the real recovery copy) is settled by the real observer, which re-opens the
`title_text` question; a typed reply goes through `submit_turn`'s free-text matcher, the gate
replays the stored answer from the real thread events, and the next render's receipts pass.

Not verified here (needs the live brief extractor): what a typed WORD reply extracts to. The
deterministic guarantees are: an option reply becomes a `choice_selection` server-side, and a
reply that is not an option is never turned into one.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria import planner
from app.kria.api_schemas import SubmitTurnBody
from app.kria.brief_binding import BriefBinding
from app.kria.brief_checks import render_block_recovery
from app.kria.runtime import submit_turn
from app.models import (
    CreationThread,
    CreationThreadEvent,
    CreatorAgentExecution,
    CreatorAgentSession,
    CreatorAgentTurn,
    Job,
    PlanItem,
)
from app.services.choice_questions import answered_brief, title_text_choice
from app.tasks.kria_runtime import _observe_dispatched_execution
from tests.kria.test_choice_title_text import (
    _blocks,
    _incident_brief,
    _planned,
    _render,
    _snapshot,
    _strategy,
)
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

GENERATION = "gen-1"


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "kria_choice_questions_enabled", True)
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", True)


def _seed_blocked_render(brief) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:  # noqa: ANN001
    """A thread whose first render was blocked on the wordless title, awaiting its observer."""
    user_id, thread_id, session_id = _seed_runtime_project()
    _, receipts = _render(brief, {})
    failures = [r for r in receipts if r["verification"] == "checked" and r["status"] != "met"]
    block = render_block_recovery(failures)
    binding = BriefBinding.create(thread_id, brief, latest_message="Make my day montage")
    stored = [{**r, "brief_version": brief.version, "generation_id": GENERATION} for r in receipts]
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id, with_for_update=True)
        session = db.get(CreatorAgentSession, session_id, with_for_update=True)
        item = db.get(PlanItem, thread.active_plan_item_id, with_for_update=True)
        source = CreationThreadEvent(
            thread_id=thread_id,
            sequence=2,
            revision=3,
            client_event_id=f"b-{uuid.uuid4().hex}",
            role="user",
            event_type="user_message",
            content="Render the approved draft",
            payload=None,
        )
        db.add(source)
        db.flush()
        turn = CreatorAgentTurn(
            thread_id=thread_id,
            session_id=session_id,
            source_event_id=source.id,
            client_event_id=source.client_event_id,
            request_digest=uuid.uuid4().hex,
            status="observing",
        )
        db.add(turn)
        db.flush()
        job = Job(
            user_id=user_id,
            status="processing_failed",
            mode="generative",
            raw_storage_path="",
            selected_platforms=["tiktok"],
            content_plan_item_id=item.id,
            content_plan_ownership_epoch=0,
            failure_reason="phone_plan_unsupported",
            error_detail=block.message,
            assembly_plan={
                "variants": [],
                "creator_generation_id": GENERATION,
                "creator_brief_binding": binding.model_dump(mode="json"),
                "creator_decline": {
                    "decline_reason": block.decline_reason,
                    "field_path": block.field_path,
                    "alternative": block.alternative,
                    "failure_reason": "phone_plan_unsupported",
                },
                "request_recovery": {
                    "message": block.message,
                    "requirement_receipts": stored,
                    "brief_version": brief.version,
                    "generation_id": GENERATION,
                    "binding_digest": binding.digest,
                },
            },
        )
        db.add(job)
        db.flush()
        execution = CreatorAgentExecution(
            session_id=session_id,
            turn_id=turn.id,
            idempotency_key=f"b-{uuid.uuid4().hex}",
            request_digest=uuid.uuid4().hex,
            expected_revision=3,
            tool_name="render.request",
            tool_version=1,
            risk="approval_required",
            target_thread_id=thread_id,
            target_job_id=job.id,
            target_ownership_epoch=0,
            status="dispatched",
            started_at=datetime.now(UTC),
            dispatched_at=datetime.now(UTC),
        )
        db.add(execution)
        item.current_job_id = job.id
        session.status = "rendering"
        session.target_job_id = job.id
        thread.active_job_id = job.id
        thread.revision = 3
        db.commit()
        execution_id = execution.id
    return user_id, thread_id, execution_id


def _failed_event(thread_id: uuid.UUID) -> CreationThreadEvent:
    with sync_session() as db:
        return (
            db.execute(
                select(CreationThreadEvent)
                .where(
                    CreationThreadEvent.thread_id == thread_id,
                    CreationThreadEvent.event_type == "assistant_render_failed",
                )
                .order_by(CreationThreadEvent.sequence.desc())
            )
            .scalars()
            .first()
        )


async def _reply(user_id, thread_id, message):  # noqa: ANN001, ANN202
    with sync_session() as db:
        revision = db.get(CreationThread, thread_id).revision
    async with AsyncSessionLocal() as db:
        await submit_turn(
            db,
            thread_id=thread_id,
            creator_id=user_id,
            body=SubmitTurnBody(
                message=message,
                client_event_id=f"r-{uuid.uuid4().hex}",
                expected_thread_revision=revision,
            ),
        )


def _selections(thread_id: uuid.UUID) -> list[dict]:
    with sync_session() as db:
        rows = db.execute(
            select(CreationThreadEvent.payload).where(
                CreationThreadEvent.thread_id == thread_id, CreationThreadEvent.role == "user"
            )
        ).scalars()
        return [p["choice_selection"] for p in rows if p and "choice_selection" in p]


async def _blocked_thread() -> tuple[uuid.UUID, uuid.UUID, dict]:
    brief = _incident_brief()
    user_id, thread_id, execution_id = _seed_blocked_render(brief)
    observed, _ = await asyncio.to_thread(_observe_dispatched_execution, execution_id)
    assert observed == "failed"
    return user_id, thread_id, _failed_event(thread_id).payload


@pytest.mark.asyncio
async def test_the_observer_reopens_the_same_title_question_on_the_failure_event() -> None:
    try:
        _, _, payload = await _blocked_thread()
        question = payload["choice_question"]
        draft_time = title_text_choice(["r2"])
        # The SAME conflict and digest as the draft-time question: one answer for both.
        assert question["conflict"] == draft_time.conflict_id == "title_text"
        assert question["input_digest"] == draft_time.input_digest
        assert [o["key"] for o in question["options"]] == ["no_title"]
        assert payload["decline_reason"] == "needs_choice"
    finally:
        await async_engine.dispose()


@pytest.mark.parametrize(
    "reply", ["continue without a title", "Continue without a title.", "no title please", "none"]
)
@pytest.mark.asyncio
async def test_an_option_reply_after_the_block_is_the_answer_and_the_next_render_passes(
    monkeypatch: pytest.MonkeyPatch, reply: str
) -> None:
    try:
        user_id, thread_id, payload = await _blocked_thread()
        await _reply(user_id, thread_id, reply)
        (selection,) = _selections(thread_id)
        assert selection["option_key"] == "no_title"
        assert selection["question_id"] == payload["choice_question"]["question_id"]

        # The next draft: the gate replays the stored answer from the REAL thread events.
        brief = _incident_brief()
        monkeypatch.setattr(planner, "load_latest_brief", AsyncMock(return_value=brief))
        monkeypatch.setattr(type(planner.settings), "creative_brief_for", lambda _s, _i: True)
        monkeypatch.setattr(type(planner.settings), "brief_binding_for", lambda _s, _i: True)
        monkeypatch.setattr(type(planner.settings), "phone_rendering_for", lambda _s, _i: True)
        planned = replace(_planned(), media_snapshot=_snapshot())
        async with AsyncSessionLocal() as db:
            gated = await planner._gate_unresolved_choices(
                db, planned, thread_id=thread_id, creator_id=user_id
            )
        strategy = _strategy(gated)  # no question: the answer is replayed
        (answer,) = strategy["choice_answers"]
        assert answer["kind"] == "title_text" and answer["option"] == "no_title"
        pinned = answered_brief(brief, strategy)
        assert "r2" not in [r.id for r in pinned.live()]
        _, receipts = _render(pinned, strategy)
        assert not _blocks(receipts)
    finally:
        await async_engine.dispose()


@pytest.mark.parametrize(
    "reply", ["Weekend away", "make it fun", "No Plans", "The hook should say 'Weekend away'"]
)
@pytest.mark.asyncio
async def test_a_reply_that_is_not_an_option_is_never_turned_into_one(reply: str) -> None:
    try:
        user_id, thread_id, _ = await _blocked_thread()
        await _reply(user_id, thread_id, reply)
        assert _selections(thread_id) == []  # it goes to the planner/extractor as plain words
    finally:
        await async_engine.dispose()
