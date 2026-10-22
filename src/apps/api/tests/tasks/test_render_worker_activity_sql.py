"""Real-Postgres contracts for the render-worker creator-activity guard."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import sync_session
from app.models import (
    ContentPlan,
    CreationThread,
    CreationThreadEvent,
    CreationThreadUploadReservation,
    CreatorAgentApproval,
    CreatorAgentEvent,
    CreatorAgentSession,
    CreatorAgentTurn,
    CreatorPlanningAttempt,
    Persona,
    PlanItem,
    User,
)
from app.tasks.maintenance import _CREATOR_ACTIVITY_SQL

_WINDOW_S = 600
_INFLIGHT_S = 3600
_STALE_AT = datetime.now(UTC) - timedelta(seconds=_INFLIGHT_S + 60)
# Parametrized times are offsets from the moment the case runs, not from
# collection: a full suite can reach these cases minutes after collection.
_NOW = timedelta(0)
_STALE = -timedelta(seconds=_INFLIGHT_S + 60)


def _at(offset: timedelta) -> datetime:
    return datetime.now(UTC) + offset


def _activity(db: Session) -> bool:
    return bool(
        db.execute(
            _CREATOR_ACTIVITY_SQL,
            {"window_s": _WINDOW_S, "inflight_s": _INFLIGHT_S},
        ).scalar_one()
    )


def _quiesce_existing_activity(db: Session) -> None:
    """Make the global query false before this rolled-back row-level case."""
    for statement in (
        "UPDATE creation_threads SET updated_at = :old WHERE status = 'active'",
        "UPDATE creation_thread_upload_reservations SET expires_at = :old",
        """UPDATE creator_planning_attempts
           SET status = 'completed', updated_at = :old
           WHERE status IN ('queued', 'running')""",
        """UPDATE creator_agent_turns
           SET status = 'completed', updated_at = :old
           WHERE status IN ('pending', 'queued', 'planning', 'executing',
                            'awaiting_approval', 'observing')""",
        """UPDATE creator_agent_approvals
           SET status = 'expired', created_at = :old, expires_at = :old
           WHERE status IN ('pending', 'approved')""",
    ):
        db.execute(text(statement), {"old": _STALE_AT})
    db.flush()


@contextmanager
def _isolated_activity_case() -> Iterator[Session]:
    """Provide a global-query-safe transaction and roll it back."""
    with sync_session() as db:
        try:
            _quiesce_existing_activity(db)
            assert _activity(db) is False
            yield db
        finally:
            db.rollback()


def _thread(db: Session, label: str) -> tuple[uuid.UUID, uuid.UUID]:
    user_id, thread_id = uuid.uuid4(), uuid.uuid4()
    db.add(User(id=user_id, email=f"activity-{label}-{user_id}@test.local"))
    db.add(
        CreationThread(
            id=thread_id,
            creator_id=user_id,
            title=f"Activity {label}",
            updated_at=_STALE_AT,
        )
    )
    db.flush()
    return user_id, thread_id


def _session_graph(
    db: Session, label: str
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Create the current FK chain used by planning attempts and approvals."""
    user_id, thread_id = _thread(db, label)
    persona = Persona(user_id=user_id, persona_status="ready", questionnaire={}, persona={})
    db.add(persona)
    db.flush()
    plan = ContentPlan(user_id=user_id, persona_id=persona.id, plan_status="ready")
    db.add(plan)
    db.flush()
    item = PlanItem(content_plan_id=plan.id, position=1, idea="Activity guard fixture")
    db.add(item)
    db.flush()
    session = CreatorAgentSession(
        creator_id=user_id,
        plan_item_id=item.id,
        status="completed",
        revision=0,
        ownership_epoch=0,
    )
    db.add(session)
    db.flush()
    agent_event = CreatorAgentEvent(
        session_id=session.id,
        sequence=0,
        revision=0,
        role="user",
        event_type="user_message",
        payload={"message": "fixture"},
    )
    db.add(agent_event)
    db.flush()
    return user_id, thread_id, item.id, session.id, agent_event.id


def _turn(
    db: Session,
    *,
    thread_id: uuid.UUID,
    session_id: uuid.UUID | None,
    status: str = "completed",
    updated_at: datetime = _STALE_AT,
) -> CreatorAgentTurn:
    source_event = CreationThreadEvent(
        thread_id=thread_id,
        sequence=0,
        revision=0,
        role="user",
        event_type="user_message",
        content="turn fixture",
    )
    db.add(source_event)
    db.flush()
    turn = CreatorAgentTurn(
        thread_id=thread_id,
        session_id=session_id,
        source_event_id=source_event.id,
        client_event_id=str(uuid.uuid4()),
        request_digest="a" * 64,
        status=status,
        updated_at=updated_at,
    )
    db.add(turn)
    db.flush()
    return turn


@pytest.mark.parametrize(
    ("updated_at", "expected"),
    [(_NOW, True), (_STALE, False)],
    ids=["recent", "stale"],
)
def test_activity_sql_distinguishes_recent_and_stale_active_thread(
    updated_at: timedelta, expected: bool
) -> None:
    with _isolated_activity_case() as db:
        _user_id, thread_id = _thread(db, "thread")
        db.execute(
            text("UPDATE creation_threads SET updated_at = :updated_at WHERE id = :id"),
            {"updated_at": _at(updated_at), "id": thread_id},
        )
        assert _activity(db) is expected


@pytest.mark.parametrize(
    ("expires_at", "expected"),
    [(timedelta(seconds=60), True), (_STALE, False)],
    ids=["live-reservation", "expired-reservation"],
)
def test_activity_sql_distinguishes_live_and_expired_upload_reservation(
    expires_at: timedelta, expected: bool
) -> None:
    with _isolated_activity_case() as db:
        user_id, thread_id = _thread(db, "upload")
        db.add(
            CreationThreadUploadReservation(
                thread_id=thread_id,
                creator_id=user_id,
                media_id=f"media-{uuid.uuid4()}",
                object_path=f"users/{user_id}/activity.mp4",
                expires_at=_at(expires_at),
            )
        )
        db.flush()
        assert _activity(db) is expected


@pytest.mark.parametrize(
    ("status", "updated_at", "expected"),
    [
        ("queued", _NOW, True),
        ("running", _NOW, True),
        ("completed", _NOW, False),
        ("queued", _STALE, False),
    ],
    ids=["queued", "running", "completed", "stale-queued"],
)
def test_activity_sql_distinguishes_planning_attempt_lifecycle(
    status: str, updated_at: timedelta, expected: bool
) -> None:
    with _isolated_activity_case() as db:
        user_id, _thread_id, item_id, session_id, event_id = _session_graph(db, "planning")
        db.add(
            CreatorPlanningAttempt(
                session_id=session_id,
                source_event_id=event_id,
                creator_id=user_id,
                plan_item_id=item_id,
                ownership_epoch=0,
                session_revision=0,
                source_digest="a" * 64,
                inputs={"fixture": True},
                status=status,
                attempts=0,
                updated_at=_at(updated_at),
            )
        )
        db.flush()
        assert _activity(db) is expected


@pytest.mark.parametrize(
    ("status", "updated_at", "expected"),
    [
        ("awaiting_approval", _NOW, True),
        ("completed", _NOW, False),
        ("awaiting_approval", _STALE, False),
    ],
    ids=["awaiting-approval", "completed", "stale-awaiting-approval"],
)
def test_activity_sql_distinguishes_turn_lifecycle(
    status: str, updated_at: timedelta, expected: bool
) -> None:
    with _isolated_activity_case() as db:
        _user_id, thread_id = _thread(db, "turn")
        _turn(db, thread_id=thread_id, session_id=None, status=status, updated_at=_at(updated_at))
        assert _activity(db) is expected


@pytest.mark.parametrize(
    ("status", "created_at", "expected"),
    [
        ("pending", _NOW, True),
        ("approved", _NOW, True),
        ("expired", _NOW, False),
        ("pending", _STALE, False),
    ],
    ids=["pending", "approved", "expired", "stale-pending"],
)
def test_activity_sql_distinguishes_approval_lifecycle(
    status: str, created_at: timedelta, expected: bool
) -> None:
    with _isolated_activity_case() as db:
        user_id, thread_id, _item_id, session_id, _event_id = _session_graph(db, "approval")
        turn = _turn(db, thread_id=thread_id, session_id=session_id)
        db.add(
            CreatorAgentApproval(
                creator_id=user_id,
                thread_id=thread_id,
                session_id=session_id,
                turn_id=turn.id,
                target_ownership_epoch=0,
                consequence_summary="Activity guard fixture",
                status=status,
                expires_at=datetime.now(UTC) + timedelta(seconds=60),
                created_at=_at(created_at),
            )
        )
        db.flush()
        assert _activity(db) is expected
