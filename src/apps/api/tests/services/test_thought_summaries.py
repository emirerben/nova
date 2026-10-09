"""Real database boundaries for native thought summaries (KRI-557)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError

from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.models import (
    ContentPlan,
    CreationThread,
    CreationThreadEvent,
    Persona,
    PlanItem,
    ThoughtSummary,
    User,
)
from app.routes.thought_summaries import (
    list_creation_thought_summaries,
    list_slide_post_thought_summaries,
)
from app.services.thought_summaries import ThoughtSummaryPublisher


@pytest.fixture
def thought_subjects():
    """Use dedicated accounts and delete them through database cascades."""
    owner_id, stranger_id = uuid.uuid4(), uuid.uuid4()
    thread_id, item_id = uuid.uuid4(), uuid.uuid4()
    persona_id = uuid.uuid4()
    try:
        with sync_session() as db:
            db.add_all(
                [
                    User(id=owner_id, email=f"thought-owner-{owner_id}@example.test"),
                    User(id=stranger_id, email=f"thought-stranger-{stranger_id}@example.test"),
                ]
            )
            db.flush()
            db.add(Persona(id=persona_id, user_id=owner_id))
            db.flush()
            plan = ContentPlan(user_id=owner_id, persona_id=persona_id)
            db.add(plan)
            db.flush()
            db.add(PlanItem(id=item_id, content_plan_id=plan.id, position=0, idea="Test slide"))
            db.add(
                CreationThread(
                    id=thread_id,
                    creator_id=owner_id,
                    content_plan_id=plan.id,
                    active_plan_item_id=item_id,
                    revision=7,
                )
            )
            db.commit()
    except OperationalError as exc:
        pytest.skip(f"Local Postgres unavailable: {type(exc).__name__}")
    try:
        yield owner_id, stranger_id, thread_id, item_id
    finally:
        with sync_session() as db:
            db.execute(
                text("DELETE FROM users WHERE id IN (:owner, :stranger)"),
                {"owner": owner_id, "stranger": stranger_id},
            )
            db.commit()


@pytest.mark.asyncio
async def test_attempt_fencing_history_ownership_and_chat_revision(
    thought_subjects, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "thought_summaries_enabled", True)
    owner_id, stranger_id, thread_id, item_id = thought_subjects
    publisher = ThoughtSummaryPublisher(
        creator_id=owner_id, thread_id=thread_id, client_request_id="request-1"
    )
    first = publisher.begin_attempt()
    first("Discard this thought")
    second = publisher.begin_attempt()
    first(" late chunk")
    second("Provider ")
    second("summary")
    publisher.complete()

    empty = ThoughtSummaryPublisher(
        creator_id=owner_id, thread_id=thread_id, client_request_id="request-empty"
    )
    empty.begin_attempt()
    empty.complete()

    failed = ThoughtSummaryPublisher(
        creator_id=owner_id, thread_id=thread_id, client_request_id="request-failed"
    )
    failed.begin_attempt()("Partial thought")
    failed.fail()

    abandoned = ThoughtSummaryPublisher(
        creator_id=owner_id, thread_id=thread_id, client_request_id="request-restarted"
    )
    late = abandoned.begin_attempt()
    late("Old worker text")
    resumed = ThoughtSummaryPublisher(
        creator_id=owner_id, thread_id=thread_id, client_request_id="request-restarted"
    )
    resumed.begin_attempt()("New worker text")
    late(" late chunk")
    abandoned.complete()
    resumed.complete()

    slide = ThoughtSummaryPublisher(
        creator_id=owner_id, plan_item_id=item_id, client_request_id="slide-request"
    )
    slide.begin_attempt()("Slide provider summary")
    slide.complete()

    with sync_session() as db:
        rows = db.scalars(select(ThoughtSummary).where(ThoughtSummary.creator_id == owner_id)).all()
        assert sorted((row.status, row.text) for row in rows if row.thread_id == thread_id) == [
            ("completed", "New worker text"),
            ("completed", "Provider summary"),
            ("failed", ""),
            ("failed", ""),
            ("failed", ""),
        ]
        assert db.scalar(select(CreationThread.revision).where(CreationThread.id == thread_id)) == 7
        assert (
            db.scalar(
                select(func.count())
                .select_from(CreationThreadEvent)
                .where(CreationThreadEvent.thread_id == thread_id)
            )
            == 0
        )

    async with AsyncSessionLocal() as db:
        owner = SimpleNamespace(id=owner_id)
        stranger = SimpleNamespace(id=stranger_id)
        creation = await list_creation_thought_summaries(thread_id, owner, None, db)
        assert [row.text for row in creation.summaries] == ["Provider summary", "New worker text"]
        assert creation.summaries[0].duration_ms is not None
        slide_history = await list_slide_post_thought_summaries(item_id, owner, None, db)
        assert [row.text for row in slide_history.summaries] == ["Slide provider summary"]
        monkeypatch.setattr(settings, "thought_summaries_enabled", False)
        assert (await list_creation_thought_summaries(thread_id, owner, None, db)).summaries == []
        assert (await list_slide_post_thought_summaries(item_id, owner, None, db)).summaries == []
        monkeypatch.setattr(settings, "thought_summaries_enabled", True)
        with pytest.raises(HTTPException) as creation_denial:
            await list_creation_thought_summaries(thread_id, stranger, None, db)
        assert creation_denial.value.status_code == 404
        with pytest.raises(HTTPException) as slide_denial:
            await list_slide_post_thought_summaries(item_id, stranger, None, db)
        assert slide_denial.value.status_code == 404

    # Creation history follows the chat's lifecycle; slide-post summaries
    # belong to their plan item and stay available independently.
    with sync_session() as db:
        db.execute(text("DELETE FROM creation_threads WHERE id = :id"), {"id": thread_id})
        db.commit()
        assert (
            db.scalar(
                select(func.count())
                .select_from(ThoughtSummary)
                .where(ThoughtSummary.thread_id == thread_id)
            )
            == 0
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(ThoughtSummary)
                .where(ThoughtSummary.plan_item_id == item_id)
            )
            == 1
        )
