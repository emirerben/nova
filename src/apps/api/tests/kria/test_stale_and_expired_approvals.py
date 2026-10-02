"""A stale or expired approval must never block a thread (real Postgres).

2026-10-01: an approval prepared against a broken project pointer could be neither approved
(stale) nor denied (the same staleness gate), never expired (only checked lazily at approve
time), and a queued follow-up turn behind it made every later message a 409.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.kria.api_schemas import ApprovalDecisionBody, SubmitTurnBody
from app.kria.runtime import (
    RuntimeFailure,
    approval_fingerprint,
    decide_approval,
    submit_turn,
)
from app.models import (
    CreationThread,
    CreationThreadEvent,
    CreatorAgentApproval,
    CreatorAgentSession,
    CreatorAgentTurn,
)
from app.tasks import kria_runtime as rt
from tests.kria.test_approval_dispatch_lock_order import _approved_strategy, _own_pool  # noqa: F401


async def _pending_with_queued_successor(monkeypatch):  # noqa: ANN001, ANN202
    user_id, thread_id, session_id, item_id, approval_id = await _approved_strategy(
        monkeypatch, approve=False
    )
    async with AsyncSessionLocal() as db:
        thread = await db.get(CreationThread, thread_id)
        accepted, _ = await submit_turn(
            db,
            thread_id=thread_id,
            creator_id=user_id,
            body=SubmitTurnBody(
                message="a follow-up while the approval waits",
                client_event_id=f"q-{uuid.uuid4().hex}",
                expected_thread_revision=int(thread.revision),
            ),
        )
    return user_id, thread_id, session_id, approval_id, accepted.turn_id


def _body(approval_id: uuid.UUID, thread_id: uuid.UUID) -> ApprovalDecisionBody:
    with sync_session() as db:
        approval = db.get(CreatorAgentApproval, approval_id)
        return ApprovalDecisionBody(
            expected_thread_revision=int(db.get(CreationThread, thread_id).revision),
            expected_draft_revision=approval.draft_revision,
            expected_approval_fingerprint=approval_fingerprint(approval),
        )


def _make_stale(session_id: uuid.UUID) -> None:
    with sync_session() as db:
        db.execute(
            update(CreatorAgentSession)
            .where(CreatorAgentSession.id == session_id)
            .values(target_job_id=uuid.uuid4() if False else None, manifest_hash="f" * 64)
        )
        db.commit()


def _turn_status(turn_id: str) -> str:
    with sync_session() as db:
        return db.get(CreatorAgentTurn, uuid.UUID(turn_id)).status


@pytest.mark.asyncio
async def test_deny_succeeds_on_a_stale_approval_and_promotes_the_queued_turn(monkeypatch) -> None:  # noqa: ANN001
    user_id, thread_id, session_id, approval_id, queued_id = await _pending_with_queued_successor(
        monkeypatch
    )
    assert _turn_status(queued_id) == "queued"
    _make_stale(session_id)
    body = _body(approval_id, thread_id)
    async with AsyncSessionLocal() as db:
        decision, successor = await decide_approval(
            db,
            thread_id=thread_id,
            approval_id=approval_id,
            creator_id=user_id,
            decision="deny",
            body=body,
        )
    assert decision.status == "denied" and successor == queued_id
    assert _turn_status(queued_id) == "pending"


@pytest.mark.asyncio
async def test_deny_also_dismisses_an_expired_approval(monkeypatch) -> None:  # noqa: ANN001
    user_id, thread_id, _session_id, approval_id, queued_id = await _pending_with_queued_successor(
        monkeypatch
    )
    with sync_session() as db:
        db.execute(
            update(CreatorAgentApproval)
            .where(CreatorAgentApproval.id == approval_id)
            .values(expires_at=datetime.now(UTC) - timedelta(minutes=5))
        )
        db.commit()
    body = _body(approval_id, thread_id)
    async with AsyncSessionLocal() as db:
        decision, successor = await decide_approval(
            db,
            thread_id=thread_id,
            approval_id=approval_id,
            creator_id=user_id,
            decision="deny",
            body=body,
        )
    assert decision.status == "denied" and successor == queued_id


@pytest.mark.asyncio
async def test_approving_a_stale_approval_cancels_it_cleanly(monkeypatch) -> None:  # noqa: ANN001
    user_id, thread_id, session_id, approval_id, queued_id = await _pending_with_queued_successor(
        monkeypatch
    )
    _make_stale(session_id)
    body = _body(approval_id, thread_id)
    async with AsyncSessionLocal() as db:
        with pytest.raises(RuntimeFailure) as caught:
            await decide_approval(
                db,
                thread_id=thread_id,
                approval_id=approval_id,
                creator_id=user_id,
                decision="approve",
                body=body,
            )
    assert caught.value.code == "approval_target_stale"
    with sync_session() as db:
        assert db.get(CreatorAgentApproval, approval_id).status == "cancelled"
        events = list(
            db.execute(
                select(CreationThreadEvent.event_type, CreationThreadEvent.content).where(
                    CreationThreadEvent.thread_id == thread_id
                )
            )
        )
    assert any(t == "assistant_error" and "cancelled it" in (c or "") for t, c in events)
    assert _turn_status(queued_id) == "pending"


@pytest.mark.asyncio
async def test_sweep_expires_an_unattended_approval_and_promotes_the_successor(monkeypatch) -> None:  # noqa: ANN001
    _user, thread_id, _session, approval_id, queued_id = await _pending_with_queued_successor(
        monkeypatch
    )
    with sync_session() as db:
        db.execute(
            update(CreatorAgentApproval)
            .where(CreatorAgentApproval.id == approval_id)
            .values(expires_at=datetime.now(UTC) - timedelta(minutes=5))
        )
        db.commit()
    published: list[str] = []
    monkeypatch.setattr(
        rt.run_kria_turn, "apply_async", lambda **kw: published.append(kw["args"][0])
    )
    monkeypatch.setattr(rt.execute_kria_approval, "apply_async", lambda **_kw: None)
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    # The shared test DB may hold other expired approvals; the sweep is bounded per run.
    out = None
    for _ in range(40):
        out = rt.reconcile_kria_turns.run()
        with sync_session() as db:
            if db.get(CreatorAgentApproval, approval_id).status == "expired":
                break
    with sync_session() as db:
        assert db.get(CreatorAgentApproval, approval_id).status == "expired", (
            out,
            db.get(CreatorAgentApproval, approval_id).execution_ids,
        )
        events = [
            c
            for (c,) in db.execute(
                select(CreationThreadEvent.content).where(
                    CreationThreadEvent.thread_id == thread_id,
                    CreationThreadEvent.event_type == "assistant_error",
                )
            )
        ]
    assert any("expired" in (c or "") for c in events)
    # (another xdist worker's sweep may have expired it first; either way it was promoted)
    assert queued_id in published or _turn_status(queued_id) in {"pending", "planning", "completed"}


def test_the_sweep_lock_is_released_after_a_run_and_after_an_exception(monkeypatch) -> None:  # noqa: ANN001
    from sqlalchemy import text

    from app.database import sync_engine

    key = 0x4B52494152454400 + (uuid.uuid4().int % 1000)
    monkeypatch.setattr(rt, "_RECONCILE_ADVISORY_KEY", key)
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)

    def free() -> bool:
        with sync_engine.connect() as conn:
            got = conn.execute(text("select pg_try_advisory_lock(:k)"), {"k": key}).scalar()
            if got:
                conn.execute(text("select pg_advisory_unlock(:k)"), {"k": key})
            conn.commit()
            return bool(got)

    rt.reconcile_kria_turns.run()
    assert free()
    monkeypatch.setattr(
        rt, "_reconcile_kria_turns_body", lambda: (_ for _ in ()).throw(RuntimeError("x"))
    )
    with pytest.raises(RuntimeError):
        rt.reconcile_kria_turns.run()
    assert free()
