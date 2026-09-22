"""Real-transaction coverage for the preparation outbox, leases and write fences."""

from __future__ import annotations

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from app.config import settings
from app.database import sync_engine, sync_session
from app.models import (
    ContentPlan,
    CreatorAgentEvent,
    CreatorAgentSession,
    CreatorPlanningAttempt,
    Persona,
    PlanItem,
    User,
)
from app.services.creator_preparation import progress, source_digest, source_snapshot
from app.tasks import creator_preparation as task
from app.tasks.autoplace import ANALYSIS_VERSION

if not (make_url(settings.database_url).database or "").endswith("_test"):
    pytest.skip("requires a dedicated test database", allow_module_level=True)
try:
    with sync_engine.connect() as conn:
        conn.execute(text("SELECT preparation FROM creator_agent_sessions LIMIT 0"))
except OperationalError:
    pytest.skip("test PostgreSQL unavailable", allow_module_level=True)


@pytest.fixture
def graph():
    uid, pid, iid, sid, aid, eid = [uuid.uuid4() for _ in range(6)]
    with sync_session() as db:
        db.add(User(id=uid, email=f"prep-{uid}@example.test"))
        db.flush()
        persona = Persona(user_id=uid, persona_status="ready", questionnaire={}, persona={})
        db.add(persona)
        db.flush()
        plan = ContentPlan(id=pid, user_id=uid, persona_id=persona.id, plan_status="ready")
        db.add(plan)
        db.flush()
        item = PlanItem(
            id=iid,
            content_plan_id=pid,
            position=1,
            idea="test",
            edit_format="montage",
            clip_assignments=[
                {
                    "media_id": "clip-1",
                    "gcs_path": f"users/{uid}/clip.mp4",
                    "storage_generation": "42",
                    "kind": "video",
                }
            ],
            clip_gcs_paths=[f"users/{uid}/clip.mp4"],
        )
        db.add(item)
        db.flush()
        session = CreatorAgentSession(
            id=sid,
            creator_id=uid,
            plan_item_id=iid,
            status="planning",
            revision=1,
            ownership_epoch=0,
            preparation=progress(aid, "queued", 0, 1),
        )
        db.add(session)
        db.flush()
        db.add(
            CreatorAgentEvent(
                id=eid,
                session_id=sid,
                sequence=0,
                revision=1,
                role="user",
                event_type="user_message",
                payload={"message": "Keep the coast clips first"},
            )
        )
        db.flush()
        db.add(
            CreatorPlanningAttempt(
                id=aid,
                session_id=sid,
                plan_item_id=iid,
                creator_id=uid,
                source_event_id=eid,
                ownership_epoch=0,
                session_revision=1,
                source_digest=source_digest(source_snapshot(item, [])),
                inputs={"user_message": "Keep the coast clips first"},
                status="queued",
                attempts=0,
            )
        )
        db.commit()
    yield uid, pid, iid, sid, aid
    with sync_session() as db:
        db.execute(delete(ContentPlan).where(ContentPlan.id == pid))
        db.execute(delete(Persona).where(Persona.user_id == uid))
        db.execute(delete(User).where(User.id == uid))
        db.commit()


def test_duplicate_delivery_only_one_claims(graph):
    aid = graph[-1]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(task._claim, [aid, aid]))
    assert sum(result is not None for result in results) == 1
    with sync_session() as db:
        assert db.get(CreatorPlanningAttempt, aid).attempts == 1


def test_checkpoint_survives_worker_loss_and_stale_lease_cannot_write(graph):
    _uid, _pid, iid, sid, aid = graph
    token, _inputs, sources, *_ = task._claim(aid)
    analyzed = {
        **sources[0],
        "generation": "42",
        "analysis": {
            "source": "clip_metadata",
            "analysis_version": ANALYSIS_VERSION,
            "subject": "coast",
        },
    }
    task._checkpoint(aid, token, analyzed)
    with sync_session() as db:
        attempt = db.get(CreatorPlanningAttempt, aid)
        attempt.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
    claimed = task._claim(aid)
    assert claimed[0] != token
    assert claimed[2][0]["analysis"]["subject"] == "coast"
    with pytest.raises(task.PreparationStale):
        task._checkpoint(aid, token, {**analyzed, "analysis": {"subject": "wrong"}})
    with sync_session() as db:
        assert db.get(PlanItem, iid).clip_assignments[0]["analysis"]["subject"] == "coast"
        assert db.get(CreatorAgentSession, sid).preparation["completed"] == 1


@pytest.mark.parametrize("mutation", ["cancel", "revision", "generation", "quarantine"])
def test_stale_results_and_failure_do_not_overwrite_current_state(graph, mutation):
    _uid, pid, iid, sid, aid = graph
    token, _inputs, sources, *_ = task._claim(aid)
    with sync_session() as db:
        session = db.get(CreatorAgentSession, sid)
        if mutation == "cancel":
            session.status = "cancelled"
        elif mutation == "revision":
            session.revision += 1
        elif mutation == "generation":
            item = db.get(PlanItem, iid)
            item.clip_assignments = [{**item.clip_assignments[0], "storage_generation": "43"}]
        else:
            db.get(ContentPlan, pid).ownership_quarantined_at = datetime.now(UTC)
        db.commit()
    with pytest.raises(task.PreparationStale):
        task._checkpoint(
            aid, token, {**sources[0], "generation": "42", "analysis": {"subject": "private"}}
        )
    task._fail(aid, token, "provider_quota_exceeded", retryable=True)
    with sync_session() as db:
        assert db.get(PlanItem, iid).clip_assignments[0].get("analysis") is None
        assert db.get(CreatorPlanningAttempt, aid).status == "superseded"
        errors = list(
            db.execute(
                select(CreatorAgentEvent).where(
                    CreatorAgentEvent.session_id == sid,
                    CreatorAgentEvent.event_type == "assistant_error",
                )
            ).scalars()
        )
        if mutation == "generation":
            assert errors[0].payload["code"] == "preparation_stale"
        else:
            assert errors == []


def test_quota_failure_keeps_request_and_safe_retry_state(graph):
    _uid, _pid, _iid, sid, aid = graph
    token, *_ = task._claim(aid)
    task._fail(aid, token, "provider_quota_exceeded", retryable=True)
    with sync_session() as db:
        session = db.get(CreatorAgentSession, sid)
        assert session.status == "briefing"
        assert session.active_plan is None
        assert session.preparation["retryable"] is True
        assert session.preparation["error_code"] == "provider_quota_exceeded"
        events = list(
            db.execute(
                select(CreatorAgentEvent)
                .where(CreatorAgentEvent.session_id == sid)
                .order_by(CreatorAgentEvent.sequence)
            ).scalars()
        )
        assert [e.sequence for e in events] == [0, 1]
        assert events[0].payload["message"] == "Keep the coast clips first"
        assert events[1].event_type == "assistant_error"
        assert (
            db.get(CreatorPlanningAttempt, aid).inputs["user_message"]
            == events[0].payload["message"]
        )


def test_reconciler_publishes_committed_outbox_and_throttles_duplicates(graph, monkeypatch):
    aid = graph[-1]
    published = []
    monkeypatch.setattr(task, "publish_preparation", published.append)
    task.reconcile_creator_preparations()
    task.reconcile_creator_preparations()
    assert published.count(str(aid)) == 1


def test_resume_uses_original_request_and_completes_receipt_atomically(graph, monkeypatch):
    from app.routes import creator_agent
    from app.services.creator_preparation import finish_preparation
    from app.services.creator_sessions import append_event

    uid, _pid, _iid, sid, aid = graph
    token, inputs, *_ = task._claim(aid)
    seen = []

    async def plan(db, **kwargs):
        seen.append(kwargs["user_message"])
        session = await db.get(CreatorAgentSession, sid)
        session.status = "awaiting_confirmation"
        await append_event(
            db, session, event_type="assistant_strategy", payload={"message": "Ready"}
        )
        await finish_preparation(db, session)
        await db.commit()

    monkeypatch.setattr(creator_agent, "_run_planning_turn", plan)
    asyncio.run(task._resume(aid, token, inputs, str(uid), str(sid)))
    assert seen == ["Keep the coast clips first"]
    assert task._claim(aid) is None
    with sync_session() as db:
        assert db.get(CreatorPlanningAttempt, aid).status == "completed"
        assert db.get(CreatorAgentSession, sid).status == "awaiting_confirmation"
