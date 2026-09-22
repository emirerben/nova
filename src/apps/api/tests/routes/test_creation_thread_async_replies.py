"""Polling must deliver background planning updates before a render exists."""

import copy
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import Response

from app.routes import creation_threads as routes


@pytest.mark.asyncio
@pytest.mark.parametrize("reply_count", [1, 101])
@pytest.mark.parametrize(
    ("status", "event_type", "preparation_status"),
    [
        ("awaiting_confirmation", "assistant_strategy", "ready"),
        ("briefing", "assistant_error", "failed"),
        ("failed", "assistant_error", "failed"),
    ],
)
async def test_poll_projects_background_reply_once_without_render_reconciliation(
    monkeypatch, status, event_type, preparation_status, reply_count
):
    user = SimpleNamespace(id=uuid.uuid4())
    session = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user.id,
        status="planning",
        phase="planning",
        revision=1,
        active_plan=None,
        preparation={"status": "queued", "completed": 5, "total": 10},
    )
    thread = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user.id,
        runtime_version=1,
        active_creator_agent_session_id=session.id,
        active_job_id=None,
        state={"creator_agent": routes._creator_agent_projection(session)},
        events=[],
        revision=11,
    )
    agent_events = []
    db = SimpleNamespace(
        get=AsyncMock(return_value=session),
        execute=AsyncMock(
            return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: agent_events))
        ),
        commit=AsyncMock(),
        refresh=AsyncMock(),
    )
    monkeypatch.setattr(routes, "_load", AsyncMock(return_value=thread))
    monkeypatch.setattr(
        routes, "_repair_stale_cleanup_failure_graph", AsyncMock(return_value=(False, None))
    )
    monkeypatch.setattr(
        routes, "_repair_missing_thread_job_projection", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(
        routes, "_response", AsyncMock(side_effect=lambda *_: copy.deepcopy(thread))
    )

    async def append(_db, target, **event):
        target.events.append(event)
        target.revision += 1

    monkeypatch.setattr(routes, "_append", append)

    # Progress changes without advancing the session revision or creating a Job.
    session.preparation = {"status": "analyzing", "completed": 8, "total": 10}
    progress = await routes.get_thread(str(thread.id), user, db, Response())
    assert progress.state["creator_agent"]["preparation"]["completed"] == 8
    db.commit.assert_awaited_once()

    # The worker finishes after the previous poll. Real render reconciliation
    # returns False for both terminal planning states; the reply still belongs
    # in the very next response, without another user message.
    session.status = session.phase = status
    session.revision = 2
    session.preparation = {"status": preparation_status, "completed": 10, "total": 10}
    session.active_plan = {"summary": "Your narrated edit", "private_operations": ["hidden"]}
    agent_events.extend(
        SimpleNamespace(
            id=uuid.uuid4(),
            event_type=event_type,
            payload={"message": "Your planning result", "private_operations": ["hidden"]},
        )
        for _ in range(reply_count)
    )
    reply = await routes.get_thread(str(thread.id), user, db, Response())
    assert reply.state["creator_agent"]["status"] == status
    assert reply.state["creator_agent"]["preparation"]["status"] == preparation_status
    assert (
        reply.events
        == [
            {
                "event_type": f"agent_{event_type}",
                "role": "assistant",
                "content": "Your planning result",
                "payload": {"message": "Your planning result"},
            }
        ]
        * reply_count
    )
    assert "private_operations" not in reply.state["creator_agent"]
    assert reply.revision == 11 + reply_count
    assert db.commit.await_count == 2

    # An unchanged poll must neither duplicate the reply nor rewrite the row.
    repeated = await routes.get_thread(str(thread.id), user, db, Response())
    assert repeated.events == reply.events
    assert repeated.revision == reply.revision
    assert db.commit.await_count == 2


@pytest.mark.asyncio
async def test_full_runtime_v2_poll_does_not_import_legacy_agent_events(monkeypatch):
    thread = SimpleNamespace(
        id=uuid.uuid4(), runtime_version=2, active_creator_agent_session_id=uuid.uuid4()
    )
    db = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(phase="planning")))
    monkeypatch.setattr(routes, "_load", AsyncMock(return_value=thread))
    monkeypatch.setattr(
        routes, "_repair_stale_cleanup_failure_graph", AsyncMock(return_value=(False, None))
    )
    monkeypatch.setattr(
        routes, "_repair_missing_thread_job_projection", AsyncMock(return_value=False)
    )
    sync = AsyncMock()
    monkeypatch.setattr(routes, "_sync_agent", sync)
    monkeypatch.setattr(routes, "_response", AsyncMock(return_value=thread))

    await routes.get_thread(
        str(thread.id), SimpleNamespace(id=uuid.uuid4()), db, Response(), projection="full"
    )

    sync.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_truncated_receipts_do_not_replay_old_replies(monkeypatch):
    user_id = uuid.uuid4()
    session = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user_id,
        status="awaiting_confirmation",
        revision=120,
        active_plan=None,
    )
    events = [
        SimpleNamespace(
            id=uuid.uuid4(),
            revision=revision,
            event_type="assistant_strategy",
            payload={"message": "A proposed edit"},
        )
        for revision in range(1, 121)
    ]
    thread = SimpleNamespace(
        creator_id=user_id,
        active_creator_agent_session_id=session.id,
        state={
            "creator_agent": routes._creator_agent_projection(session),
            "creator_agent_event_ids": [str(event.id) for event in events[:100]],
        },
    )
    db = SimpleNamespace(
        get=AsyncMock(return_value=session),
        execute=AsyncMock(
            return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: events))
        ),
    )
    append = AsyncMock()
    monkeypatch.setattr(routes, "_append", append)

    assert await routes._sync_agent(db, thread) is True
    append.assert_not_awaited()
    assert len(thread.state["creator_agent_event_ids"]) == 120
    assert await routes._sync_agent(db, thread) is False

    # A new reply with identical copy still has its own source revision.
    session.revision = 121
    events.append(
        SimpleNamespace(
            id=uuid.uuid4(),
            revision=121,
            event_type="assistant_strategy",
            payload={"message": "A proposed edit"},
        )
    )
    assert await routes._sync_agent(db, thread) is True
    append.assert_awaited_once()

    # Replacing a terminal session must not reuse its higher revision fence.
    session.id = thread.active_creator_agent_session_id = uuid.uuid4()
    session.revision = 1
    events[:] = [
        SimpleNamespace(
            id=uuid.uuid4(),
            revision=1,
            event_type="assistant_question",
            payload={"message": "What next?"},
        )
    ]
    assert await routes._sync_agent(db, thread) is True
    assert append.await_count == 2
