"""A finished or failed render's chat card lands within seconds, not on the minute sweep.

Café M2 (2026-10-21): job 50f67a06 failed at 13:05:12 (``phone_guided_job_failed``) but its
"Your video wasn't made" card was posted at 13:05:53, because only the minute sweep
(``reconcile_kria_turns``) settled dispatched receipts. Each test drives the real worker /
route / approval path and the real observer against Postgres; only the AI planner and the
broker publish are stubbed.

Failure modes these tests pin (written before the change):
1. The render ends -> the render task itself nudges the observer; the card is posted by the
   nudge, and the sweep afterwards finds nothing left to settle.
2. The render fails BEFORE its receipt is committed ``dispatched`` -> the dispatch nudges too.
3. Two observers (nudge + sweep, or two nudges) race on one receipt -> exactly one card.
4. The phone reports a failed export -> the route nudges; the card names the iPhone.
5. A nudge that finds the Job still rendering changes nothing.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from contextlib import nullcontext
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.agents._schemas.creator_agent import CreativeStrategy, ProposeStrategy
from app.auth import get_current_user
from app.config import settings
from app.database import AsyncSessionLocal, get_db, sync_engine, sync_session
from app.database import engine as async_engine
from app.kria.api_schemas import ApprovalDecisionBody, SubmitTurnBody
from app.kria.device_render import make_device_request
from app.kria.planner import PlannedKriaTurn, adapt_creator_action
from app.kria.recipes import EditRecipeV1
from app.kria.runtime import approval_fingerprint, decide_approval, submit_turn
from app.main import app
from app.models import (
    ContentPlan,
    CreationThread,
    CreationThreadEvent,
    CreatorAgentApproval,
    CreatorAgentExecution,
    CreatorAgentSession,
    CreatorAgentTurn,
    Job,
    PlanItem,
)
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan
from app.routes import device_render as device_routes
from app.services.device_render import pin_device_request
from app.services.phone_sources import PHONE_SOURCES_FIELD
from app.tasks import generative_build as gb
from app.tasks import kria_runtime as rt
from app.tasks.content_plan_build import DispatchResult
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project, _seed_user_turn

# The planner's refusal from the café M2 worker log (job 50f67a06).
CAFE_REFUSAL = (
    "some of the groups you named are not where you said This draft is about 4.7s; you asked "
    "for 14s. Your draft is saved. Should I try again or simplify this request?"
)


@pytest.fixture(autouse=True)
def _runtime_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)


@pytest.fixture
def nudges(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """The broker publishes of the observer nudge (the task itself is run explicitly)."""
    published: list[dict] = []
    monkeypatch.setattr(rt.observe_kria_job, "apply_async", lambda **kw: published.append(kw))
    monkeypatch.setattr(rt.run_kria_turn, "apply_async", lambda **_kw: None)
    return published


def _seed_dispatched_render(*, job_status: str, assembly_plan: dict) -> SimpleNamespace:
    """A runtime-v2 chat whose approved render is dispatched and awaiting its observer."""
    user_id, thread_id, session_id = _seed_runtime_project()
    turn_id = _seed_user_turn(thread_id, session_id, content="Generate", status="observing")
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        session = db.get(CreatorAgentSession, session_id)
        job = Job(
            user_id=user_id,
            status=job_status,
            mode="content_plan",
            raw_storage_path="",
            selected_platforms=["tiktok"],
            content_plan_item_id=session.plan_item_id,
            content_plan_ownership_epoch=0,
            all_candidates={},
            assembly_plan=assembly_plan,
        )
        db.add(job)
        db.flush()
        session.target_job_id = job.id
        session.status = "rendering"
        db.get(PlanItem, session.plan_item_id).current_job_id = job.id
        execution = CreatorAgentExecution(
            session_id=session_id,
            turn_id=turn_id,
            idempotency_key=uuid.uuid4().hex,
            request_digest=uuid.uuid4().hex,
            expected_revision=thread.revision,
            tool_name="render.request",
            tool_version=1,
            risk="approval_required",
            target_thread_id=thread_id,
            target_job_id=job.id,
            target_ownership_epoch=0,
            status="dispatched",
            dispatched_at=datetime.now(UTC),
            result={"job_id": str(job.id)},
        )
        db.add(execution)
        db.commit()
        return SimpleNamespace(
            user_id=user_id,
            thread_id=thread_id,
            session_id=session_id,
            turn_id=turn_id,
            job_id=job.id,
            execution_id=execution.id,
            plan_item_id=session.plan_item_id,
        )


def _failure_cards(thread_id: uuid.UUID) -> list[CreationThreadEvent]:
    with sync_session() as db:
        return list(
            db.execute(
                select(CreationThreadEvent).where(
                    CreationThreadEvent.thread_id == thread_id,
                    CreationThreadEvent.event_type == "assistant_render_failed",
                )
            ).scalars()
        )


def _sweep_only(monkeypatch: pytest.MonkeyPatch, execution_id: uuid.UUID) -> None:
    """Run the real sweep observer for this test's receipt; leave other tests' rows alone."""
    real = rt._observe_dispatched_execution

    def observe(candidate: uuid.UUID) -> tuple[str, str | None]:
        return real(candidate) if candidate == execution_id else ("ignored", None)

    monkeypatch.setattr(rt, "_observe_dispatched_execution", observe)
    monkeypatch.setattr(rt.execute_kria_approval, "apply_async", lambda **_kw: None)


def test_failed_phone_plan_render_posts_its_card_from_the_render_task_nudge(
    monkeypatch: pytest.MonkeyPatch, nudges: list[dict]
) -> None:
    seeded = _seed_dispatched_render(
        job_status="queued",
        assembly_plan={
            PHONE_SOURCES_FIELD: [],
            "creator_generation_id": "generation-cafe",
            "guided_edit": {"approved": True},
        },
    )
    monkeypatch.setattr(settings, "phone_rendering_enabled", True)

    def refuse(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise UnsupportedPhonePlan(CAFE_REFUSAL)

    # Only the AI planner is stubbed; dispatch, `_fail_job` and the commit are the real worker.
    monkeypatch.setattr(gb, "_guided_execution_plan", refuse)
    monkeypatch.setattr("app.services.pipeline_trace.pipeline_trace_for", lambda _: nullcontext())
    monkeypatch.setattr(gb, "job_heartbeat", lambda _: nullcontext())

    gb.orchestrate_generative_job.run(str(seeded.job_id))

    with sync_session() as db:
        job = db.get(Job, seeded.job_id)
        assert job.status == "processing_failed"
        assert job.failure_reason == "phone_plan_unsupported"
    assert nudges == [
        {"args": [str(seeded.job_id)], "queue": "agent-control", "expires": rt.OBSERVE_NUDGE_TTL_S}
    ]
    # The nudge only enqueues: nothing is posted until the observer task runs.
    assert _failure_cards(seeded.thread_id) == []

    assert rt.observe_kria_job.run(str(seeded.job_id)) == {"settled": 1}

    [card] = _failure_cards(seeded.thread_id)
    assert card.payload["code"] == "phone_plan_unsupported"
    assert card.payload["recovery"] == "ask_user"
    assert card.payload["execution_id"] == str(seeded.execution_id)
    with sync_session() as db:
        assert db.get(CreatorAgentExecution, seeded.execution_id).status == "failed"
        assert db.get(CreatorAgentTurn, seeded.turn_id).status == "failed"

    # The minute sweep is still the fallback, and finds nothing left to post.
    _sweep_only(monkeypatch, seeded.execution_id)
    assert rt.reconcile_kria_turns.run()["settled"] == 0
    assert len(_failure_cards(seeded.thread_id)) == 1


def test_nudge_for_a_render_still_in_progress_changes_nothing(nudges: list[dict]) -> None:
    seeded = _seed_dispatched_render(job_status="processing", assembly_plan={"variants": []})

    assert rt.observe_kria_job.run(str(seeded.job_id)) == {"settled": 0}

    assert _failure_cards(seeded.thread_id) == []
    with sync_session() as db:
        assert db.get(CreatorAgentExecution, seeded.execution_id).status == "dispatched"
        assert db.get(CreatorAgentTurn, seeded.turn_id).status == "observing"


def test_nudge_and_sweep_racing_on_one_receipt_post_one_card(nudges: list[dict]) -> None:
    seeded = _seed_dispatched_render(job_status="processing_failed", assembly_plan={"variants": []})
    with sync_session() as db:
        db.get(Job, seeded.job_id).failure_reason = "phone_plan_unsupported"
        db.commit()
        plan_id = db.get(PlanItem, seeded.plan_item_id).content_plan_id

    # Hold the plan row (the observer's first lock) so both observers read the receipt as
    # `dispatched` BEFORE either can lock it: the interleaving that could post twice.
    results: list[dict] = []
    with sync_engine.connect() as holder:
        holder.execute(
            select(ContentPlan.id).where(ContentPlan.id == plan_id).with_for_update()
        ).all()
        threads = [
            threading.Thread(
                target=lambda: results.append(rt.observe_kria_job.run(str(seeded.job_id)))
            )
            for _ in range(2)
        ]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + 10
        with sync_engine.connect() as probe:
            while time.monotonic() < deadline:
                waiting = probe.execute(
                    text(
                        "select count(*) from pg_stat_activity "
                        "where wait_event_type = 'Lock' and datname = current_database()"
                    )
                ).scalar_one()
                probe.rollback()
                if waiting >= 2:
                    break
                time.sleep(0.05)
            else:
                pytest.fail("both observers never queued on the plan lock")
        holder.rollback()
    for thread in threads:
        thread.join(timeout=10)

    assert sorted(result["settled"] for result in results) == [0, 1]
    assert len(_failure_cards(seeded.thread_id)) == 1


@pytest.mark.asyncio
async def test_render_that_fails_before_its_receipt_is_dispatched_still_gets_a_fast_card(
    monkeypatch: pytest.MonkeyPatch, nudges: list[dict]
) -> None:
    """The render task's own nudge found no `dispatched` receipt yet; the dispatch nudges."""
    user_id, thread_id, session_id = _seed_runtime_project()
    strategy_plan = adapt_creator_action(
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

    async def _planned(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        return PlannedKriaTurn(plan=strategy_plan, manifest_hash="a" * 64, context_hash="b" * 64)

    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(rt, "_plan_with_live_agent", _planned)
    try:
        async with AsyncSessionLocal() as db:
            accepted, _ = await submit_turn(
                db,
                thread_id=thread_id,
                creator_id=user_id,
                body=SubmitTurnBody(
                    message="Make the matcha update feel quick but personal",
                    client_event_id=f"strategy-{uuid.uuid4().hex}",
                    expected_thread_revision=2,
                ),
            )
        await asyncio.to_thread(rt.run_kria_turn.run, accepted.turn_id)
        with sync_session() as db:
            approval = db.execute(
                select(CreatorAgentApproval).where(
                    CreatorAgentApproval.turn_id == uuid.UUID(accepted.turn_id)
                )
            ).scalar_one()
            approval_id, token = approval.id, approval_fingerprint(approval)
            draft_revision = approval.draft_revision
            thread_revision = db.get(CreationThread, thread_id).revision
            item_id = db.get(CreatorAgentSession, session_id).plan_item_id
        async with AsyncSessionLocal() as db:
            await decide_approval(
                db,
                thread_id=thread_id,
                approval_id=approval_id,
                creator_id=user_id,
                decision="approve",
                body=ApprovalDecisionBody(
                    expected_thread_revision=thread_revision,
                    expected_draft_revision=draft_revision,
                    expected_approval_fingerprint=token,
                ),
            )

        def _dispatch_that_fails_at_once(*_args, **_kwargs) -> DispatchResult:  # noqa: ANN002, ANN003
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
                job_id = str(job.id)
            # The worker refused the plan before `_finish_approval_dispatch` committed.
            assert gb._fail_job(job_id, CAFE_REFUSAL, failure_reason="phone_plan_unsupported")
            return DispatchResult("dispatched", job_id=job_id)

        monkeypatch.setattr(
            "app.tasks.content_plan_build.dispatch_item_render_for", _dispatch_that_fails_at_once
        )
        dispatched = await asyncio.to_thread(rt.execute_kria_approval.run, str(approval_id))
        assert dispatched["status"] == "dispatched"
        job_id = dispatched["job_id"]

        assert [nudge["args"] for nudge in nudges] == [[job_id]]
        assert _failure_cards(thread_id) == []
        assert await asyncio.to_thread(rt.observe_kria_job.run, job_id) == {"settled": 1}
        [card] = _failure_cards(thread_id)
        assert card.payload["code"] == "phone_plan_unsupported"
        assert card.payload["job_id"] == job_id
    finally:
        await async_engine.dispose()


def test_phone_reported_export_failure_posts_its_card_from_the_route_nudge(
    monkeypatch: pytest.MonkeyPatch, nudges: list[dict]
) -> None:
    seeded = _seed_dispatched_render(job_status="awaiting_device", assembly_plan={})
    recipe = EditRecipeV1(
        assets=[{"id": "a", "relative_path": "a"}],
        tracks=[
            {
                "id": "v",
                "kind": "video",
                "clips": [
                    {
                        "id": "c",
                        "source_asset_id": "a",
                        "source_start": 0,
                        "source_duration": 2,
                        "timeline_start": 0,
                        "rate": 1,
                    }
                ],
            }
        ],
    )
    with sync_session() as db:
        job = db.get(Job, seeded.job_id)
        job.assembly_plan = {
            "creator_generation_id": "approved",
            "variants": [
                {
                    "variant_id": "guided_story",
                    "render_status": "awaiting_device",
                    "render_destination": "device",
                    "render_generation_id": "approved",
                }
            ],
        }
        request = make_device_request(
            job_id=job.id, variant_id="guided_story", revision=1, recipe=recipe
        )
        pin_device_request(job, request, base_generation="approved")
        db.commit()
    # Before the phone reports, a nudge finds the device render still pending.
    assert rt.observe_kria_job.run(str(seeded.job_id)) == {"settled": 0}

    engine = create_async_engine(settings.asyncpg_database_url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def real_db():  # noqa: ANN202
        async with sessions() as db:
            yield db

    monkeypatch.setattr(device_routes.limiter, "enabled", False)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=seeded.user_id)
    app.dependency_overrides[get_db] = real_db
    try:
        with TestClient(app) as client:
            response = client.post(
                f"/me/jobs/{seeded.job_id}/device-render/failures",
                json={
                    "identity": request.identity.model_dump(mode="json"),
                    "reason_code": "export_failed",
                    "detail": "",
                },
            )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200, response.text
    assert [nudge["args"] for nudge in nudges] == [[str(seeded.job_id)]]

    assert rt.observe_kria_job.run(str(seeded.job_id)) == {"settled": 1}
    [card] = _failure_cards(seeded.thread_id)
    assert card.payload["code"] == "device_render_failed"
    assert "iPhone" in card.content
    with sync_session() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(CreatorAgentExecution)
                .where(
                    CreatorAgentExecution.target_job_id == seeded.job_id,
                    CreatorAgentExecution.status == "dispatched",
                )
            )
            == 0
        )
