"""KRI-1558 (Tadic, #prs 2026-11-26): the advertised sections feature reaches the render.

A protocol number alone can belong to another branch's build without the sections renderer, so
the planner plans speaker sections only for a build that advertised `speakerSections` in
`X-Kria-Client-Features`. That only works if what the build advertised crosses the real
HTTP-service / Celery boundary intact, the same path as the protocol stamp
(`test_kri898_freeze_protocol_stamp_postgres.py`):

1. Submit records what the sending build advertised on the message (`client_features`).
2. Approve stashes what BOTH the drafting and the approving build advertised
   (`approval_client_features`): a build can only narrow what the draft was planned for.
3. Dispatch hands it to the Job (`creator_client_features`); nothing advertised = no kwarg, so
   the call is byte-identical to before and no sections are planned.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select

from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria.api_schemas import ApprovalDecisionBody, SubmitTurnBody
from app.kria.planner import PlannedKriaTurn
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
from app.services.client_protocol import (
    SPEAKER_SECTIONS_PROTOCOL,
    clear_request_context,
    set_client_features,
    set_client_protocol,
)
from app.tasks.content_plan_build import DispatchResult
from app.tasks.kria_runtime import execute_kria_approval, run_kria_turn
from tests.kria.test_kri898_freeze_protocol_stamp_postgres import (
    _runtime_enabled,  # noqa: F401 - autouse fixture
    _strategy_plan,
)
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

SECTIONS = "speakerSections"


def _build(features: str | None) -> None:
    set_client_protocol(SPEAKER_SECTIONS_PROTOCOL)
    set_client_features(features)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sent", "approved", "render_for"),
    [
        (SECTIONS, SECTIONS, [SECTIONS]),
        (f"clipDim,{SECTIONS}", SECTIONS, [SECTIONS]),
        (SECTIONS, None, None),
        (None, SECTIONS, None),
        ("clipDim", "clipDim", ["clipDim"]),
    ],
    ids=[
        "both-advertise",
        "drafted-with-more",
        "approved-by-a-build-without",
        "drafted-by-a-build-without",
        "other-features-only",
    ],
)
async def test_what_both_builds_advertised_reaches_the_job(
    monkeypatch: pytest.MonkeyPatch,
    sent: str | None,
    approved: str | None,
    render_for: list[str] | None,
) -> None:
    user_id, thread_id, session_id = _seed_runtime_project()
    body = SubmitTurnBody(
        message="Put my pictures behind me and change where I am part by part",
        client_event_id=f"kri1558-{uuid.uuid4().hex}",
        expected_thread_revision=2,
    )

    async def _planned(snapshot, *_args, **_kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        return PlannedKriaTurn(plan=_strategy_plan(), manifest_hash="a" * 64, context_hash="b" * 64)

    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", _planned)
    dispatched_with: list[dict] = []
    try:
        _build(sent)
        async with AsyncSessionLocal() as db:
            accepted, _ = await submit_turn(db, thread_id=thread_id, creator_id=user_id, body=body)
        with sync_session() as db:
            turn = db.get(CreatorAgentTurn, uuid.UUID(accepted.turn_id))
            source = db.get(CreationThreadEvent, turn.source_event_id)
            stored = source.payload.get("client_features")
            assert stored == (sorted(sent.split(",")) if sent else None)

        clear_request_context()
        result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
        assert result == {"turn_id": accepted.turn_id, "status": "awaiting_approval"}

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

        _build(approved)
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
    assert kwargs["creator_client_protocol"] == SPEAKER_SECTIONS_PROTOCOL
    if render_for is None:
        assert "creator_client_features" not in kwargs
    else:
        assert kwargs["creator_client_features"] == render_for
