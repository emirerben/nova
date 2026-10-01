"""Real-Postgres reproduction of the 2026-10-01 approval deadlock and its fallout.

`_finish_approval_dispatch` took the Session lock first and then pointed the session and the
execution at the NEW Job (a foreign key: FOR KEY SHARE on the Job row). The status poll
(`_lock_reconciliation_graph`) locks PlanItem -> Job (FOR UPDATE, the new current job) and
THEN wants the Session. The two form a cycle that no static order check can see, because the
second lock in `_finish` is implicit (the FK). Postgres killed one of them; the worker task
died after `dispatch_item_render_for` had already moved the plan item to the new Job, leaving
the project pointing at an unrendered orphan with a stale approval.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.agents._schemas.creator_agent import CreativeStrategy, ProposeStrategy
from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.kria.api_schemas import ApprovalDecisionBody, SubmitTurnBody
from app.kria.planner import PlannedKriaTurn, adapt_creator_action
from app.kria.runtime import approval_fingerprint, decide_approval, submit_turn
from app.models import (
    CreationThread,
    CreatorAgentApproval,
    CreatorAgentExecution,
    CreatorAgentSession,
    Job,
    PlanItem,
)
from app.tasks.content_plan_build import DispatchResult
from app.tasks.kria_runtime import execute_kria_approval, run_kria_turn
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project


@pytest_asyncio.fixture(autouse=True)
async def _own_pool():  # noqa: ANN202
    """Pooled asyncpg connections belong to one event loop: never leak mine to other tests."""
    from app.database import engine as async_engine

    await async_engine.dispose()
    yield
    await async_engine.dispose()


async def _approved_strategy(monkeypatch: pytest.MonkeyPatch, *, approve: bool = True):  # noqa: ANN202
    user_id, thread_id, session_id = _seed_runtime_project()
    plan = adapt_creator_action(
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
            summary="Open on the whisk.",
        )
    )

    async def planned(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        return PlannedKriaTurn(plan=plan, manifest_hash="a" * 64, context_hash="b" * 64)

    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", planned)
    async with AsyncSessionLocal() as db:
        accepted, _ = await submit_turn(
            db,
            thread_id=thread_id,
            creator_id=user_id,
            body=SubmitTurnBody(
                message="Make the matcha update feel quick",
                client_event_id=f"lock-{uuid.uuid4().hex}",
                expected_thread_revision=2,
            ),
        )
    await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
    with sync_session() as db:
        approval = db.execute(
            select(CreatorAgentApproval).where(
                CreatorAgentApproval.turn_id == uuid.UUID(accepted.turn_id)
            )
        ).scalar_one()
        approval_id, token, rev = (
            approval.id,
            approval_fingerprint(approval),
            approval.draft_revision,
        )
        revision = db.get(CreationThread, thread_id).revision
        item_id = db.get(CreatorAgentSession, session_id).plan_item_id
    if not approve:
        return user_id, thread_id, session_id, item_id, approval_id
    async with AsyncSessionLocal() as db:
        await decide_approval(
            db,
            thread_id=thread_id,
            approval_id=approval_id,
            creator_id=user_id,
            decision="approve",
            body=ApprovalDecisionBody(
                expected_thread_revision=revision,
                expected_draft_revision=rev,
                expected_approval_fingerprint=token,
            ),
        )
    return user_id, thread_id, session_id, item_id, approval_id


def _mint_job_like_dispatch(user_id: uuid.UUID, item_id: uuid.UUID) -> DispatchResult:
    with sync_session() as db:
        item = db.get(PlanItem, item_id, with_for_update=True)
        job = Job(
            user_id=user_id,
            status="awaiting_device",
            mode="content_plan",
            raw_storage_path="",
            selected_platforms=["tiktok"],
            content_plan_item_id=item.id,
            content_plan_ownership_epoch=0,
            assembly_plan={"variants": []},
        )
        db.add(job)
        db.flush()
        item.current_job_id = job.id
        item.item_status = "awaiting_clips"
        db.commit()
        return DispatchResult("dispatched", job_id=str(job.id))


@pytest.mark.asyncio
async def test_status_poll_holding_the_new_job_does_not_deadlock_the_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, _thread_id, session_id, item_id, approval_id = await _approved_strategy(monkeypatch)
    poll_error: list[BaseException] = []
    ready = threading.Event()

    def poll(new_job_id: uuid.UUID) -> None:
        """What GET /creation-threads/{id} does: PlanItem -> Job (new current job) -> Session."""
        try:
            with sync_session() as db:
                db.execute(select(PlanItem).where(PlanItem.id == item_id).with_for_update())
                db.execute(select(Job).where(Job.id == new_job_id).with_for_update())
                ready.set()
                time.sleep(0.6)  # the finish is now blocked on the Job row (pre-fix)
                db.execute(
                    select(CreatorAgentSession)
                    .where(CreatorAgentSession.id == session_id)
                    .with_for_update()
                )
                db.commit()
        except BaseException as exc:  # noqa: BLE001
            poll_error.append(exc)
            ready.set()

    def dispatch(*_a, **_k) -> DispatchResult:  # noqa: ANN002, ANN003
        result = _mint_job_like_dispatch(user_id, item_id)
        threading.Thread(target=poll, args=(uuid.UUID(result.job_id),), daemon=True).start()
        assert ready.wait(5)
        return result

    monkeypatch.setattr("app.tasks.content_plan_build.dispatch_item_render_for", dispatch)
    try:
        out = await asyncio.to_thread(execute_kria_approval.run, str(approval_id))
    except OperationalError as exc:  # pre-fix: DeadlockDetected raised out of the task
        pytest.fail(f"deadlock: {exc.orig}")
    assert out["status"] == "dispatched" and not poll_error
    with sync_session() as db:
        execution = db.execute(
            select(CreatorAgentExecution).where(
                CreatorAgentExecution.id
                == uuid.UUID(db.get(CreatorAgentApproval, approval_id).execution_ids[0])
            )
        ).scalar_one()
        assert execution.status == "dispatched"


@pytest.mark.asyncio
async def test_a_failed_finish_restores_the_plan_item_and_fails_the_orphan_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, thread_id, session_id, item_id, approval_id = await _approved_strategy(monkeypatch)
    with sync_session() as db:
        old_job = Job(
            user_id=user_id,
            status="variants_ready",
            mode="content_plan",
            raw_storage_path="",
            selected_platforms=["tiktok"],
            content_plan_item_id=item_id,
            content_plan_ownership_epoch=0,
            assembly_plan={"variants": []},
        )
        db.add(old_job)
        db.flush()
        item = db.get(PlanItem, item_id)
        item.current_job_id, item.item_status = old_job.id, "ready"
        session = db.get(CreatorAgentSession, session_id)
        session.target_job_id = old_job.id
        approval = db.get(CreatorAgentApproval, approval_id)
        approval.target_job_id = old_job.id
        db.commit()
        old_id = old_job.id

    monkeypatch.setattr(
        "app.tasks.content_plan_build.dispatch_item_render_for",
        lambda *_a, **_k: _mint_job_like_dispatch(user_id, item_id),
    )
    import app.tasks.kria_runtime as rt

    real_finish = rt._finish_approval_dispatch
    calls: list[str] = []

    def exploding_finish(claim, **kw):  # noqa: ANN001, ANN003, ANN202
        calls.append(kw["outcome"])
        if kw["outcome"] == "dispatched":
            raise RuntimeError("boom after the pointer moved")
        return real_finish(claim, **kw)

    monkeypatch.setattr(rt, "_finish_approval_dispatch", exploding_finish)
    out = await asyncio.to_thread(execute_kria_approval.run, str(approval_id))
    assert calls == ["dispatched", "publish_failed"]
    assert out["status"] == "failed"
    with sync_session() as db:
        item = db.get(PlanItem, item_id)
        assert item.current_job_id == old_id and item.item_status == "ready"
        orphans = list(
            db.execute(
                select(Job).where(Job.content_plan_item_id == item_id, Job.id != old_id)
            ).scalars()
        )
        assert [j.status for j in orphans] == ["failed"]
        execution = db.execute(
            select(CreatorAgentExecution).where(
                CreatorAgentExecution.id
                == uuid.UUID(db.get(CreatorAgentApproval, approval_id).execution_ids[0])
            )
        ).scalar_one()
        assert execution.status == "failed" and execution.error["retryable"] is True


def test_reconcile_skips_when_another_sweep_holds_the_advisory_lock(monkeypatch) -> None:  # noqa: ANN001
    from sqlalchemy import text

    import app.tasks.kria_runtime as rt
    from app.database import sync_engine

    # A private key: other tests' sweeps (xdist shares the database) must not be skipped.
    key = 0x4B52494152454301 + (uuid.uuid4().int % 1000)
    monkeypatch.setattr(rt, "_RECONCILE_ADVISORY_KEY", key)
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    with sync_engine.connect() as holder:
        assert holder.execute(text("select pg_try_advisory_lock(:k)"), {"k": key}).scalar()
        try:
            out = rt.reconcile_kria_turns.run()
        finally:
            holder.execute(text("select pg_advisory_unlock(:k)"), {"k": key})
            holder.commit()
    assert out.get("skipped") == 1
