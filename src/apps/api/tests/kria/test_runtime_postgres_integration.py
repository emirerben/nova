"""Real-Postgres integration coverage for the Kria runtime-v2 durability seam.

These tests intentionally cross the async HTTP-service/sync Celery boundary.
Mocks cannot prove that one accepted turn survives the handoff, owns a database
lease, writes a real execution receipt, and is observable through the delta API.
Each test uses fresh UUID rows, so the module is xdist-safe without truncation.
"""

from __future__ import annotations

import asyncio
import copy
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError

from app.agents._schemas.creator_agent import CreativeStrategy, ProposeStrategy
from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria.api_schemas import ApprovalDecisionBody, SubmitTurnBody
from app.kria.brief import BriefUpdate
from app.kria.contracts import KriaTurnPlan
from app.kria.drafts import read_or_bootstrap_draft, undo_draft, write_draft
from app.kria.planner import PlannedKriaTurn, adapt_creator_action, adapt_editor_action
from app.kria.runtime import (
    RuntimeFailure,
    approval_fingerprint,
    decide_approval,
    read_delta,
    submit_turn,
)
from app.models import (
    ContentPlan,
    CreationThread,
    CreationThreadEvent,
    CreativeBriefVersion,
    CreatorAgentApproval,
    CreatorAgentExecution,
    CreatorAgentSession,
    CreatorAgentTurn,
    CreatorEditDraft,
    Job,
    Persona,
    PlanItem,
    User,
)
from app.routes.creator_agent import get_creator_session
from app.tasks.content_plan_build import DispatchResult
from app.tasks.kria_runtime import (
    _claim,
    _claim_approval_dispatch,
    _complete_response_turn,
    _observe_dispatched_execution,
    _plan_with_live_agent,
    execute_kria_approval,
    prune_kria_drafts,
    reconcile_kria_turns,
    run_kria_turn,
)

_db_name = make_url(settings.database_url).database or ""
if not _db_name.endswith("_test"):
    pytest.skip(f"refusing to write to non-test database {_db_name!r}", allow_module_level=True)
try:
    with sync_session() as _probe:
        _probe.execute(text("select 1"))
except OperationalError:
    pytest.skip("nova_test Postgres not reachable", allow_module_level=True)


@pytest.fixture(autouse=True)
def _runtime_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)


def _seed_runtime_project() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    user_id = uuid.uuid4()
    persona_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    item_id = uuid.uuid4()
    session_id = uuid.uuid4()
    thread_id = uuid.uuid4()
    with sync_session() as db:
        db.add(User(id=user_id, email=f"{user_id}@test.local"))
        db.flush()
        db.add(
            Persona(
                id=persona_id,
                user_id=user_id,
                persona_status="ready",
                persona={"content_mode": "travel", "tone": "direct"},
            )
        )
        db.flush()
        db.add(ContentPlan(id=plan_id, user_id=user_id, persona_id=persona_id))
        db.flush()
        db.add(
            PlanItem(
                id=item_id,
                content_plan_id=plan_id,
                position=1,
                idea="A matcha-making diary",
                item_status="awaiting_clips",
            )
        )
        db.flush()
        db.add(CreatorAgentSession(id=session_id, creator_id=user_id, plan_item_id=item_id))
        db.flush()
        db.add(
            CreationThread(
                id=thread_id,
                creator_id=user_id,
                runtime_version=2,
                content_plan_id=plan_id,
                active_plan_item_id=item_id,
                active_creator_agent_session_id=session_id,
                title="Matcha diary",
                revision=2,
                state={
                    "media": [{"media_id": "clip-1", "filename": "whisking.mov"}],
                    "media_count": 1,
                    "edit_format": "day_vlog",
                },
            )
        )
        db.flush()
        db.add_all(
            [
                CreationThreadEvent(
                    thread_id=thread_id,
                    sequence=0,
                    revision=1,
                    role="system",
                    event_type="thread_created",
                    content=None,
                    payload=None,
                ),
                CreationThreadEvent(
                    thread_id=thread_id,
                    sequence=1,
                    revision=2,
                    role="assistant",
                    event_type="format_prompt",
                    content="What are we making?",
                    payload={"kind": "select_format"},
                ),
            ]
        )
        db.commit()
    return user_id, thread_id, session_id


@pytest.mark.asyncio
async def test_submit_worker_receipt_and_delta_are_durable_across_real_sessions() -> None:
    user_id, thread_id, session_id = _seed_runtime_project()
    body = SubmitTurnBody(
        message="Find the strongest opening in my footage",
        client_event_id=f"integration-{uuid.uuid4().hex}",
        expected_thread_revision=2,
    )

    try:
        async with AsyncSessionLocal() as db:
            accepted, should_publish = await submit_turn(
                db,
                thread_id=thread_id,
                creator_id=user_id,
                body=body,
            )

        assert accepted.status == "pending"
        assert accepted.thread_revision == 3
        assert should_publish is True

        # A lost HTTP response can replay from a fresh SQLAlchemy session.
        # Values must be copied before rollback expires the ORM rows.
        async with AsyncSessionLocal() as db:
            replayed, replay_should_publish = await submit_turn(
                db,
                thread_id=thread_id,
                creator_id=user_id,
                body=body,
            )
        assert replayed.turn_id == accepted.turn_id
        assert replayed.thread_revision == accepted.thread_revision
        assert replayed.status == "pending"
        assert replayed.replayed is True
        assert replay_should_publish is True

        task_result = run_kria_turn.run(accepted.turn_id)
        assert task_result == {"turn_id": accepted.turn_id, "status": "completed"}

        with sync_session() as db:
            turn = db.get(CreatorAgentTurn, uuid.UUID(accepted.turn_id))
            assert turn is not None
            assert turn.status == "completed"
            assert turn.session_id == session_id
            assert turn.observed_event_id is not None
            assert turn.plan_json is not None
            receipt = db.execute(
                select(CreatorAgentExecution).where(CreatorAgentExecution.turn_id == turn.id)
            ).scalar_one()
            assert receipt.session_id == session_id
            assert receipt.status == "completed"
            assert receipt.tool_name == "project.inspect"
            assert receipt.tool_version == 1
            assert receipt.risk == "read"
            assert receipt.result is not None
            assert receipt.result["available_media"] == ["whisking.mov"]

        async with AsyncSessionLocal() as db:
            delta = await read_delta(
                db,
                thread_id=thread_id,
                creator_id=user_id,
                after_sequence=1,
                limit=20,
            )

        assert delta.thread_revision == 4
        assert [event.event_type for event in delta.events] == [
            "user_message",
            "assistant_response",
        ]
        observed = delta.events[-1]
        assert observed.content == (
            "Open with whisking.mov; it gives the story an immediate visual point of view."
        )
        assert observed.payload is not None
        assert observed.payload["receipt_ids"] == [str(receipt.id)]
        assert delta.next_after_sequence == 3
        assert delta.has_more is False
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_legacy_creator_agent_route_cannot_load_runtime_v2_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, thread_id, _session_id = _seed_runtime_project()
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        assert thread is not None
        item_id = thread.active_plan_item_id
    assert item_id is not None
    monkeypatch.setattr("app.routes.creator_agent._require_feature", lambda *_args, **_kwargs: None)

    try:
        async with AsyncSessionLocal() as db:
            with pytest.raises(HTTPException, match="new Kria runtime") as failure:
                await get_creator_session(
                    str(item_id),
                    User(id=user_id, email=f"{user_id}@test.local"),
                    db,
                )
        assert failure.value.status_code == 409
    finally:
        await async_engine.dispose()


def test_runtime_version_trigger_rejects_controller_owner_changes() -> None:
    _user_id, thread_id, _session_id = _seed_runtime_project()

    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        assert thread is not None
        thread.runtime_version = 1
        with pytest.raises(DBAPIError, match="runtime_version is immutable"):
            db.commit()
        db.rollback()

        persisted = db.get(CreationThread, thread_id)
        assert persisted is not None
        assert persisted.runtime_version == 2


def test_partial_unique_index_allows_only_one_active_turn_per_thread() -> None:
    _user_id, thread_id, session_id = _seed_runtime_project()

    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        assert thread is not None
        thread.revision = 3
        first_event = CreationThreadEvent(
            thread_id=thread_id,
            sequence=2,
            revision=3,
            role="user",
            event_type="user_message",
            content="First active turn",
        )
        db.add(first_event)
        db.flush()
        db.add(
            CreatorAgentTurn(
                thread_id=thread_id,
                session_id=session_id,
                source_event_id=first_event.id,
                client_event_id=f"first-{uuid.uuid4().hex}",
                request_digest="first-digest",
                status="planning",
            )
        )
        db.commit()

        thread = db.get(CreationThread, thread_id)
        assert thread is not None
        thread.revision = 4
        second_event = CreationThreadEvent(
            thread_id=thread_id,
            sequence=3,
            revision=4,
            role="user",
            event_type="user_message",
            content="Second active turn",
        )
        db.add(second_event)
        db.flush()
        db.add(
            CreatorAgentTurn(
                thread_id=thread_id,
                session_id=session_id,
                source_event_id=second_event.id,
                client_event_id=f"second-{uuid.uuid4().hex}",
                request_digest="second-digest",
                status="planning",
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        active_count = db.scalar(
            select(func.count())
            .select_from(CreatorAgentTurn)
            .where(CreatorAgentTurn.thread_id == thread_id, CreatorAgentTurn.status == "planning")
        )
        assert active_count == 1


def test_live_planner_session_survives_consecutive_task_event_loops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every `run_kria_turn` plans inside its own `asyncio.run` loop, and an
    asyncpg connection belongs to the loop that opened it. The second turn a
    Celery child planned used to check out the first turn's pooled connection
    and die on "attached to a different loop" (prod turn 932db9f6, 2026-09-24).
    """

    async def _planned(db, **_kwargs):  # noqa: ANN001, ANN003, ANN202
        assert await db.scalar(text("select 1")) == 1
        return PlannedKriaTurn(
            plan=KriaTurnPlan(mode="respond", turn_value="question", response="ok"),
            manifest_hash="manifest",
            context_hash="context",
        )

    monkeypatch.setattr("app.tasks.kria_runtime.plan_live_turn", _planned)
    snapshot = {
        "thread_id": str(uuid.uuid4()),
        "item_id": str(uuid.uuid4()),
        "creator_id": str(uuid.uuid4()),
    }
    for _turn in range(2):
        planned = asyncio.run(
            _plan_with_live_agent(
                snapshot,
                "Plan this edit",
                turn_id=uuid.uuid4(),
                lease_owner="celery-child",
                lease_epoch=1,
            )
        )
        assert planned.plan.response == "ok"


# The reconciler sweeps the 50 oldest recoverable turns. Seeding the turn under
# test far in the past keeps it inside that window however many rows earlier
# tests left behind in this shared database.
_SWEPT_FIRST = datetime(2000, 1, 1, tzinfo=UTC)


def _seed_user_turn(
    thread_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    content: str,
    status: str,
    created_at: datetime | None = None,
) -> uuid.UUID:
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        assert thread is not None
        thread.revision = int(thread.revision) + 1
        sequence = db.scalar(
            select(func.max(CreationThreadEvent.sequence)).where(
                CreationThreadEvent.thread_id == thread_id
            )
        )
        event = CreationThreadEvent(
            thread_id=thread_id,
            sequence=int(sequence) + 1,
            revision=thread.revision,
            role="user",
            event_type="user_message",
            content=content,
        )
        db.add(event)
        db.flush()
        turn = CreatorAgentTurn(
            thread_id=thread_id,
            session_id=session_id,
            source_event_id=event.id,
            client_event_id=f"turn-{uuid.uuid4().hex}",
            request_digest=f"digest-{uuid.uuid4().hex}",
            status=status,
            **({"created_at": created_at} if created_at is not None else {}),
        )
        db.add(turn)
        db.commit()
        return turn.id


def _lapse_lease(turn_id: uuid.UUID) -> None:
    """The run holding the lease is gone (killed at the task time limit, lost
    worker), so its heartbeat stopped and the lease ran out."""
    with sync_session() as db:
        db.execute(
            update(CreatorAgentTurn)
            .where(CreatorAgentTurn.id == turn_id)
            .values(lease_expires_at=func.now() - timedelta(seconds=1))
        )
        db.commit()


def _record_publishes(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Capture broker publishes and keep the reconciler off other tests' rows."""
    published: list[list[str]] = []
    monkeypatch.setattr(run_kria_turn, "apply_async", lambda **kw: published.append(kw["args"]))
    monkeypatch.setattr(execute_kria_approval, "apply_async", lambda **_kw: None)
    monkeypatch.setattr(
        "app.tasks.kria_runtime._observe_dispatched_execution",
        lambda _execution_id: ("dispatched", None),
    )
    return published


@pytest.mark.parametrize("recovered_by", ["reconciler", "redelivered_task"])
def test_turn_abandoned_three_times_fails_and_promotes_its_successor(
    monkeypatch: pytest.MonkeyPatch,
    recovered_by: str,
) -> None:
    """A turn whose runs keep ending without a result is not planned a fourth
    time: every run can pay for a Main Creator call, and the reconciler would
    republish it forever while the creator's next message waits behind it."""
    _user_id, thread_id, session_id = _seed_runtime_project()
    stuck_id = _seed_user_turn(
        thread_id,
        session_id,
        content="Top 3 players, pop up each photo",
        status="pending",
        created_at=_SWEPT_FIRST,
    )
    queued_id = _seed_user_turn(thread_id, session_id, content="Add a ding", status="queued")
    published = _record_publishes(monkeypatch)

    def _never_plan(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("a turn out of claims must not be planned again")

    for attempt in range(1, 4):
        # Claims 2 and 3 recover a lapsed lease: via the reconciler's reset, or
        # directly when a redelivered task reaches the row first.
        claimed = _claim(stuck_id, f"worker-{attempt}")
        assert isinstance(claimed, tuple)
        assert claimed[2] == attempt
        _lapse_lease(stuck_id)
        if recovered_by == "reconciler":
            reconcile_kria_turns.run()

    published.clear()
    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", _never_plan)
    assert run_kria_turn.run(str(stuck_id)) == {"turn_id": str(stuck_id), "status": "failed"}

    with sync_session() as db:
        stuck = db.get(CreatorAgentTurn, stuck_id)
        assert stuck is not None
        assert stuck.status == "failed"
        assert stuck.abandoned_claims == 3
        assert stuck.lease_epoch == 3
        assert stuck.lease_owner is None
        assert stuck.lease_expires_at is None
        assert stuck.error == {
            "code": "runtime_turn_claims_exhausted",
            "retryable": True,
            "recovery": "retry",
        }
        event = db.get(CreationThreadEvent, stuck.observed_event_id)
        assert event is not None
        assert event.event_type == "assistant_error"
        assert event.content.startswith("I couldn't finish that step")
        assert event.payload["code"] == "runtime_turn_claims_exhausted"
        queued = db.get(CreatorAgentTurn, queued_id)
        assert queued is not None
        assert queued.status == "pending"
    assert published == [[str(queued_id)]]


def test_turn_requeued_past_the_cap_and_stamped_while_waiting_is_still_claimed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither a requeue after a thread-revision conflict (media attached while
    Kria planned) nor the reconciler's backoff stamp on a pending row waiting in
    a backed-up agent-control queue is an abandoned run, although the first
    bumps `lease_epoch` and the second sets `lease_expires_at`."""
    _user_id, thread_id, session_id = _seed_runtime_project()
    turn_id = _seed_user_turn(
        thread_id, session_id, content="Plan this edit", status="pending", created_at=_SWEPT_FIRST
    )
    _record_publishes(monkeypatch)
    plan = KriaTurnPlan(mode="respond", turn_value="question", response="Which moment matters?")

    for attempt in range(1, 6):
        claimed = _claim(turn_id, f"worker-{attempt}")
        assert isinstance(claimed, tuple)
        with sync_session() as db:
            thread = db.get(CreationThread, thread_id)
            assert thread is not None
            thread.revision = int(thread.revision) + 1
            db.commit()
        completion = _complete_response_turn(
            turn_id,
            lease_owner=f"worker-{attempt}",
            lease_epoch=claimed[2],
            claimed_thread_revision=claimed[3],
            plan=plan,
        )
        assert completion.requeue_turn_id == str(turn_id)
        reconcile_kria_turns.run()
        with sync_session() as db:
            turn = db.get(CreatorAgentTurn, turn_id)
            assert turn is not None
            assert turn.status == "pending"
            assert turn.lease_expires_at is not None

    claimed = _claim(turn_id, "worker-6")

    assert isinstance(claimed, tuple)
    assert claimed[2] == 6
    with sync_session() as db:
        turn = db.get(CreatorAgentTurn, turn_id)
        assert turn is not None
        assert turn.status == "planning"
        assert turn.abandoned_claims == 0


@pytest.mark.asyncio
async def test_live_planner_strategy_creates_draft_and_separate_pinned_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, thread_id, session_id = _seed_runtime_project()
    body = SubmitTurnBody(
        message="Make the matcha update feel quick but personal",
        client_event_id=f"strategy-{uuid.uuid4().hex}",
        expected_thread_revision=2,
    )
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
        return PlannedKriaTurn(
            plan=strategy_plan,
            manifest_hash="a" * 64,
            context_hash="b" * 64,
        )

    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", _planned)

    try:
        async with AsyncSessionLocal() as db:
            accepted, _ = await submit_turn(
                db,
                thread_id=thread_id,
                creator_id=user_id,
                body=body,
            )

        result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
        assert result == {"turn_id": accepted.turn_id, "status": "awaiting_approval"}

        with sync_session() as db:
            turn = db.get(CreatorAgentTurn, uuid.UUID(accepted.turn_id))
            assert turn is not None
            assert turn.status == "awaiting_approval"
            drafts = list(
                db.execute(
                    select(CreatorEditDraft).where(
                        CreatorEditDraft.source_execution_id.in_(
                            select(CreatorAgentExecution.id).where(
                                CreatorAgentExecution.turn_id == turn.id
                            )
                        )
                    )
                ).scalars()
            )
            assert len(drafts) == 1
            assert drafts[0].snapshot_json["kind"] == "strategy"
            approval = db.execute(
                select(CreatorAgentApproval).where(CreatorAgentApproval.turn_id == turn.id)
            ).scalar_one()
            assert approval.status == "pending"
            assert approval.draft_id == drafts[0].id
            assert approval.target_manifest_hash == "a" * 64
            assert (
                timedelta(minutes=29)
                < approval.expires_at - datetime.now(UTC)
                <= timedelta(minutes=30)
            )
            receipts = list(
                db.execute(
                    select(CreatorAgentExecution)
                    .where(CreatorAgentExecution.turn_id == turn.id)
                    .order_by(CreatorAgentExecution.group_order)
                ).scalars()
            )
            assert [row.status for row in receipts] == ["completed", "awaiting_approval"]
            assert receipts[0].session_id == session_id

            approval_id = approval.id
            approval_token = approval_fingerprint(approval)
            thread_revision = db.get(CreationThread, thread_id).revision
            item_id = db.get(CreatorAgentSession, session_id).plan_item_id

        async with AsyncSessionLocal() as db:
            decision, successor_id = await decide_approval(
                db,
                thread_id=thread_id,
                approval_id=approval_id,
                creator_id=user_id,
                decision="approve",
                body=ApprovalDecisionBody(
                    expected_thread_revision=thread_revision,
                    expected_draft_revision=approval.draft_revision,
                    expected_approval_fingerprint=approval_token,
                ),
            )
        assert decision.status == "approved"
        assert decision.render_dispatched is False
        assert successor_id is None

        def _fake_dispatch(*_args, **_kwargs) -> DispatchResult:  # noqa: ANN002, ANN003
            with sync_session() as db:
                item = db.get(PlanItem, item_id, with_for_update=True)
                assert item is not None
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

        monkeypatch.setattr(
            "app.tasks.content_plan_build.dispatch_item_render_for",
            _fake_dispatch,
        )
        dispatched = await asyncio.to_thread(execute_kria_approval.run, str(approval_id))
        assert dispatched["status"] == "dispatched"
        assert dispatched["job_id"] is not None

        with sync_session() as db:
            approval = db.get(CreatorAgentApproval, approval_id)
            turn = db.get(CreatorAgentTurn, uuid.UUID(accepted.turn_id))
            session = db.get(CreatorAgentSession, session_id)
            render_execution = db.execute(
                select(CreatorAgentExecution).where(
                    CreatorAgentExecution.id == uuid.UUID(approval.execution_ids[0])
                )
            ).scalar_one()
            assert approval.status == "consumed"
            assert approval.consumed_at is not None
            assert render_execution.status == "dispatched"
            assert render_execution.target_job_id == uuid.UUID(dispatched["job_id"])
            assert turn.status == "observing"
            assert session.status == "rendering"
            assert session.target_job_id == uuid.UUID(dispatched["job_id"])
            event_types = list(
                db.execute(
                    select(CreationThreadEvent.event_type)
                    .where(CreationThreadEvent.thread_id == thread_id)
                    .order_by(CreationThreadEvent.sequence)
                ).scalars()
            )
            assert event_types[-1] == "render_queued"

            execution_id = render_execution.id
            job_id = render_execution.target_job_id

        with sync_session() as db:
            job = db.get(Job, job_id, with_for_update=True)
            assert job is not None
            job.status = "variants_ready"
            job.assembly_plan = {
                **(job.assembly_plan or {}),
                "variants": [
                    {
                        "variant_id": "original_text",
                        "render_generation_id": "generation-1",
                        "render_status": "ready",
                        "output_url": "https://example.test/output.mp4",
                    }
                ],
            }
            db.commit()

        observed, promoted = await asyncio.to_thread(
            _observe_dispatched_execution,
            execution_id,
        )
        assert observed == "completed"
        assert promoted is None

        with sync_session() as db:
            turn = db.get(CreatorAgentTurn, uuid.UUID(accepted.turn_id))
            session = db.get(CreatorAgentSession, session_id)
            render_execution = db.get(CreatorAgentExecution, execution_id)
            assert turn.status == "completed"
            assert render_execution.status == "completed"
            assert render_execution.result["outcome"] == "ready"
            assert session.status == "awaiting_feedback"
            assert session.target_variant_id == "original_text"
            assert session.target_generation_id == "generation-1"
            terminal_events = list(
                db.execute(
                    select(CreationThreadEvent)
                    .where(CreationThreadEvent.thread_id == thread_id)
                    .order_by(CreationThreadEvent.sequence.desc())
                    .limit(2)
                ).scalars()
            )
            assert [row.event_type for row in terminal_events] == [
                "assistant_review",
                "generation_ready",
            ]
            assert terminal_events[0].payload["receipt_ids"] == [str(execution_id)]
            assert terminal_events[0].payload["render_generation_id"] == "generation-1"
    finally:
        await async_engine.dispose()


def _seed_narration_ready_project() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """A project whose strategy draft resolves a REAL enforce-mode narration source.

    ``audio_strategy="voiceover"`` with a recorded voiceover already on the
    item resolves an active ``voiceover`` narration source once
    `mutate_plan_item_media` sets `edit_format="narrated"` /
    `audio_mode="voiceover"` at approval time -- exactly the KRI-205 phone
    cohort scenario (a Talking/Narrated item with a narration source), just
    with a plain (non-proxy) storage path so it also exercises the ordinary
    cloud cohort path.
    """

    user_id = uuid.uuid4()
    persona_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    item_id = uuid.uuid4()
    session_id = uuid.uuid4()
    thread_id = uuid.uuid4()
    with sync_session() as db:
        db.add(User(id=user_id, email=f"{user_id}@test.local"))
        db.flush()
        db.add(
            Persona(
                id=persona_id,
                user_id=user_id,
                persona_status="ready",
                persona={"content_mode": "travel", "tone": "direct"},
            )
        )
        db.flush()
        db.add(ContentPlan(id=plan_id, user_id=user_id, persona_id=persona_id))
        db.flush()
        db.add(
            PlanItem(
                id=item_id,
                content_plan_id=plan_id,
                position=1,
                idea="A matcha-making diary",
                item_status="awaiting_clips",
                voiceover_gcs_path="users/private/matcha-voiceover.wav",
                voiceover_generation="voiceover-generation-1",
                voiceover_duration_s=30.0,
            )
        )
        db.flush()
        db.add(CreatorAgentSession(id=session_id, creator_id=user_id, plan_item_id=item_id))
        db.flush()
        db.add(
            CreationThread(
                id=thread_id,
                creator_id=user_id,
                runtime_version=2,
                content_plan_id=plan_id,
                active_plan_item_id=item_id,
                active_creator_agent_session_id=session_id,
                title="Matcha diary",
                revision=2,
                status="active",
            )
        )
        db.commit()
    return user_id, thread_id, session_id, item_id


@pytest.mark.asyncio
async def test_strategy_approval_gates_on_speech_cleanup_then_dispatch_receives_the_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KRI-205 full path: approve (aware, no choice) -> 409 ask_user, approval
    stays pending, a real analysis is scheduled and committed; mark it ready
    with findings; approve again (aware, choice=keep_original) -> approved,
    stashed on the execution; claim reads the stash back; dispatch receives
    the exact `speech_cleanup_analysis_id`/`speech_cleanup_choice` kwargs.
    """
    from app.models import SpeechCleanupAnalysis

    monkeypatch.setattr(settings, "speech_cleanup_preflight_mode", "enforce")
    monkeypatch.setattr(settings, "speech_cleanup_preflight_rollout_percent", 100)
    user_id, thread_id, session_id, item_id = _seed_narration_ready_project()
    body = SubmitTurnBody(
        message="Make the matcha update feel personal, narrated over the footage",
        client_event_id=f"cleanup-{uuid.uuid4().hex}",
        expected_thread_revision=2,
    )
    strategy_plan = adapt_creator_action(
        ProposeStrategy(
            kind="propose_strategy",
            strategy=CreativeStrategy(
                direction="guided_story",
                edit_format="narrated",
                audio_strategy="voiceover",
                pacing="fast",
                render_program="guided",
                selected_media_ids=[],
                rationale="Narrate over the whisk and packed order.",
            ),
            summary="Narrate over the whisk and the packed order.",
        )
    )

    async def _planned(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        return PlannedKriaTurn(
            plan=strategy_plan,
            manifest_hash="c" * 64,
            context_hash="d" * 64,
        )

    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", _planned)

    try:
        async with AsyncSessionLocal() as db:
            accepted, _ = await submit_turn(
                db,
                thread_id=thread_id,
                creator_id=user_id,
                body=body,
            )
        result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
        assert result == {"turn_id": accepted.turn_id, "status": "awaiting_approval"}

        with sync_session() as db:
            turn = db.get(CreatorAgentTurn, uuid.UUID(accepted.turn_id))
            approval = db.execute(
                select(CreatorAgentApproval).where(CreatorAgentApproval.turn_id == turn.id)
            ).scalar_one()
            approval_id = approval.id
            approval_token = approval_fingerprint(approval)
            thread_revision = db.get(CreationThread, thread_id).revision
            draft_revision = approval.draft_revision

        # --- 1) aware, no choice: refused, approval stays pending, a real
        # analysis row is scheduled and committed under enforce mode. ---
        async with AsyncSessionLocal() as db:
            with pytest.raises(RuntimeFailure) as failure:
                await decide_approval(
                    db,
                    thread_id=thread_id,
                    approval_id=approval_id,
                    creator_id=user_id,
                    decision="approve",
                    body=ApprovalDecisionBody(
                        expected_thread_revision=thread_revision,
                        expected_draft_revision=draft_revision,
                        expected_approval_fingerprint=approval_token,
                        speech_cleanup_aware=True,
                    ),
                )
        assert failure.value.status_code == 409
        assert failure.value.phase == "approval"
        assert failure.value.recovery == "ask_user"
        # No analysis existed before this very call (it schedules its own), so
        # an aware client submitting no id is correctly told the identity is
        # new -- exactly the same code v1's fence returns for that case.
        assert failure.value.code == "speech_cleanup_analysis_changed"

        with sync_session() as db:
            approval = db.get(CreatorAgentApproval, approval_id)
            assert approval.status == "pending"
            item = db.get(PlanItem, item_id)
            assert item.edit_format == "narrated"
            assert item.audio_mode == "voiceover"
            analysis = db.execute(
                select(SpeechCleanupAnalysis).where(
                    SpeechCleanupAnalysis.plan_item_id == item_id,
                    SpeechCleanupAnalysis.superseded_at.is_(None),
                )
            ).scalar_one()
            assert analysis.status == "queued"
            # --- Simulate the worker finishing the check with findings. ---
            analysis.status = "ready"
            analysis.candidate_count = 2
            db.commit()
            analysis_id = analysis.id

        # --- 2) aware, valid choice: approved; the choice is stashed on the
        # render execution for the claim to read back. The first attempt
        # never flipped approval/draft/thread state, so the SAME precomputed
        # pins are still valid -- this is a genuine retry of one approval. ---
        async with AsyncSessionLocal() as db:
            decision, successor_id = await decide_approval(
                db,
                thread_id=thread_id,
                approval_id=approval_id,
                creator_id=user_id,
                decision="approve",
                body=ApprovalDecisionBody(
                    expected_thread_revision=thread_revision,
                    expected_draft_revision=draft_revision,
                    expected_approval_fingerprint=approval_token,
                    speech_cleanup_aware=True,
                    speech_cleanup_analysis_id=analysis_id,
                    speech_cleanup_choice="keep_original",
                ),
            )
        assert decision.status == "approved"
        assert successor_id is None

        with sync_session() as db:
            approval = db.get(CreatorAgentApproval, approval_id)
            assert approval.status == "consumed" or approval.status == "approved"
            execution = db.execute(
                select(CreatorAgentExecution).where(
                    CreatorAgentExecution.id == uuid.UUID(approval.execution_ids[0])
                )
            ).scalar_one()
            assert execution.result["speech_cleanup"] == {
                "analysis_id": str(analysis_id),
                "choice": "keep_original",
            }

        # --- 3) the claim reads the stash back and dispatch receives it. ---
        captured: dict[str, object] = {}

        def _fake_dispatch(*args, **kwargs):  # noqa: ANN002, ANN003
            captured["args"] = args
            captured["kwargs"] = kwargs
            with sync_session() as db:
                item = db.get(PlanItem, item_id, with_for_update=True)
                assert item is not None
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

        monkeypatch.setattr(
            "app.tasks.content_plan_build.dispatch_item_render_for",
            _fake_dispatch,
        )
        dispatched = await asyncio.to_thread(execute_kria_approval.run, str(approval_id))
        assert dispatched["status"] == "dispatched"
        assert captured["kwargs"]["speech_cleanup_analysis_id"] == str(analysis_id)
        assert captured["kwargs"]["speech_cleanup_choice"] == "keep_original"
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_denying_a_speech_cleanup_gated_strategy_restores_the_rejected_media(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A "Change direction" deny after a 409 must not strand the item.

    approve(aware, no choice) -> 409, commits `edit_format="narrated"` /
    `audio_mode="voiceover"` and schedules a real analysis, approval stays
    pending -> deny -- the PlanItem must be restored to its PRE-strategy
    values (here, the untouched column defaults) and the analysis this
    rejected direction scheduled must be superseded, not left dangling.
    """
    from app.models import SpeechCleanupAnalysis

    monkeypatch.setattr(settings, "speech_cleanup_preflight_mode", "enforce")
    monkeypatch.setattr(settings, "speech_cleanup_preflight_rollout_percent", 100)
    user_id, thread_id, session_id, item_id = _seed_narration_ready_project()
    body = SubmitTurnBody(
        message="Make the matcha update feel personal, narrated over the footage",
        client_event_id=f"cleanup-deny-{uuid.uuid4().hex}",
        expected_thread_revision=2,
    )
    strategy_plan = adapt_creator_action(
        ProposeStrategy(
            kind="propose_strategy",
            strategy=CreativeStrategy(
                direction="guided_story",
                edit_format="narrated",
                audio_strategy="voiceover",
                pacing="fast",
                render_program="guided",
                selected_media_ids=[],
                rationale="Narrate over the whisk and packed order.",
            ),
            summary="Narrate over the whisk and the packed order.",
        )
    )

    async def _planned(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        return PlannedKriaTurn(
            plan=strategy_plan,
            manifest_hash="e" * 64,
            context_hash="f" * 64,
        )

    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", _planned)

    try:
        with sync_session() as db:
            original_item = db.get(PlanItem, item_id)
            original_edit_format = original_item.edit_format
            original_audio_mode = original_item.audio_mode
            original_caption_style = original_item.voiceover_caption_style
            original_user_edited = original_item.user_edited

        async with AsyncSessionLocal() as db:
            accepted, _ = await submit_turn(
                db,
                thread_id=thread_id,
                creator_id=user_id,
                body=body,
            )
        result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
        assert result == {"turn_id": accepted.turn_id, "status": "awaiting_approval"}

        with sync_session() as db:
            turn = db.get(CreatorAgentTurn, uuid.UUID(accepted.turn_id))
            approval = db.execute(
                select(CreatorAgentApproval).where(CreatorAgentApproval.turn_id == turn.id)
            ).scalar_one()
            approval_id = approval.id
            approval_token = approval_fingerprint(approval)
            thread_revision = db.get(CreationThread, thread_id).revision
            draft_revision = approval.draft_revision

        # --- 1) aware, no choice: refused; commits the strategy's media
        # mutation and schedules a real analysis, approval stays pending. ---
        async with AsyncSessionLocal() as db:
            with pytest.raises(RuntimeFailure):
                await decide_approval(
                    db,
                    thread_id=thread_id,
                    approval_id=approval_id,
                    creator_id=user_id,
                    decision="approve",
                    body=ApprovalDecisionBody(
                        expected_thread_revision=thread_revision,
                        expected_draft_revision=draft_revision,
                        expected_approval_fingerprint=approval_token,
                        speech_cleanup_aware=True,
                    ),
                )

        with sync_session() as db:
            item = db.get(PlanItem, item_id)
            assert item.edit_format == "narrated"
            assert item.audio_mode == "voiceover"
            mutated_analysis = db.execute(
                select(SpeechCleanupAnalysis).where(
                    SpeechCleanupAnalysis.plan_item_id == item_id,
                    SpeechCleanupAnalysis.superseded_at.is_(None),
                )
            ).scalar_one()
            assert mutated_analysis.status == "queued"

        # --- 2) "Change direction": deny the SAME approval (its pins never
        # changed, so the original precomputed fingerprint is still valid). ---
        async with AsyncSessionLocal() as db:
            decision, successor_id = await decide_approval(
                db,
                thread_id=thread_id,
                approval_id=approval_id,
                creator_id=user_id,
                decision="deny",
                body=ApprovalDecisionBody(
                    expected_thread_revision=thread_revision,
                    expected_draft_revision=draft_revision,
                    expected_approval_fingerprint=approval_token,
                ),
            )
        assert decision.status == "denied"

        with sync_session() as db:
            approval = db.get(CreatorAgentApproval, approval_id)
            turn = db.get(CreatorAgentTurn, uuid.UUID(accepted.turn_id))
            assert approval.status == "denied"
            assert turn.status == "completed"

            item = db.get(PlanItem, item_id)
            assert item.edit_format == original_edit_format
            assert item.audio_mode == original_audio_mode
            assert item.voiceover_caption_style == original_caption_style
            assert item.user_edited == original_user_edited

            # The rejected direction's analysis must not dangle as "current".
            still_current = db.execute(
                select(SpeechCleanupAnalysis).where(
                    SpeechCleanupAnalysis.plan_item_id == item_id,
                    SpeechCleanupAnalysis.superseded_at.is_(None),
                )
            ).scalar_one_or_none()
            assert still_current is None
            superseded = db.get(SpeechCleanupAnalysis, mutated_analysis.id)
            assert superseded.superseded_at is not None

            execution = db.execute(
                select(CreatorAgentExecution).where(
                    CreatorAgentExecution.id == uuid.UUID(approval.execution_ids[0])
                )
            ).scalar_one()
            assert "strategy_media_before" not in (execution.result or {})
        assert successor_id is None
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("job_status", "assembly_plan", "failure_reason", "expected_code"),
    [
        ("processing_failed", {"variants": []}, "ffmpeg_failed", "ffmpeg_failed"),
        (
            "variants_ready",
            {"variants": []},
            None,
            "render_identity_mismatch",
        ),
    ],
)
async def test_terminal_observer_preserves_failed_render_and_exact_identity_recovery(
    job_status: str,
    assembly_plan: dict,
    failure_reason: str | None,
    expected_code: str,
) -> None:
    user_id, thread_id, session_id = _seed_runtime_project()

    try:
        with sync_session() as db:
            thread = db.get(CreationThread, thread_id, with_for_update=True)
            session = db.get(CreatorAgentSession, session_id, with_for_update=True)
            assert thread is not None
            assert session is not None
            item = db.get(PlanItem, thread.active_plan_item_id, with_for_update=True)
            assert item is not None

            source_event = CreationThreadEvent(
                thread_id=thread_id,
                sequence=2,
                revision=3,
                client_event_id=f"observer-failure-{uuid.uuid4().hex}",
                role="user",
                event_type="user_message",
                content="Render the approved draft",
                payload=None,
            )
            db.add(source_event)
            db.flush()
            turn = CreatorAgentTurn(
                thread_id=thread_id,
                session_id=session_id,
                source_event_id=source_event.id,
                client_event_id=source_event.client_event_id,
                request_digest=uuid.uuid4().hex,
                status="observing",
            )
            db.add(turn)
            db.flush()
            job = Job(
                user_id=user_id,
                status=job_status,
                mode="generative",
                raw_storage_path="",
                selected_platforms=["tiktok"],
                content_plan_item_id=item.id,
                content_plan_ownership_epoch=0,
                assembly_plan=assembly_plan,
                failure_reason=failure_reason,
            )
            db.add(job)
            db.flush()
            execution = CreatorAgentExecution(
                session_id=session_id,
                turn_id=turn.id,
                idempotency_key=f"observer-failure-{uuid.uuid4().hex}",
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

        observed, promoted = await asyncio.to_thread(
            _observe_dispatched_execution,
            execution_id,
        )
        assert observed == "failed"
        assert promoted is None

        with sync_session() as db:
            turn = db.get(CreatorAgentTurn, turn.id)
            session = db.get(CreatorAgentSession, session_id)
            execution = db.get(CreatorAgentExecution, execution_id)
            assert turn is not None
            assert session is not None
            assert execution is not None
            assert turn.status == "failed"
            assert turn.error == {
                "code": expected_code,
                "retryable": True,
                "recovery": "retry",
            }
            assert execution.status == "failed"
            assert execution.error == turn.error
            assert execution.observed_event_id == turn.observed_event_id
            assert session.status == "awaiting_feedback"
            assert session.last_error == turn.error
            event = db.get(CreationThreadEvent, turn.observed_event_id)
            assert event is not None
            assert event.event_type == "assistant_render_failed"
            assert event.payload["receipt_ids"] == [str(execution_id)]
            assert event.payload["code"] == expected_code
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_authoritative_draft_cas_and_head_only_undo_across_sessions() -> None:
    user_id, thread_id, _session_id = _seed_runtime_project()

    try:
        async with AsyncSessionLocal() as db:
            initial = await read_or_bootstrap_draft(db, thread_id=thread_id, creator_id=user_id)

        assert initial.draft_revision == 0
        assert initial.snapshot["kind"] == "initial"
        assert initial.can_undo is False

        changed_snapshot = {
            **initial.snapshot,
            "intent": "Open on the whisk, then reveal the finished matcha.",
            "changes": ["Tighter opening", "Product reveal last"],
        }
        async with AsyncSessionLocal() as db:
            changed = await write_draft(
                db,
                thread_id=thread_id,
                creator_id=user_id,
                expected_revision=initial.draft_revision,
                expected_etag=initial.etag,
                snapshot=changed_snapshot,
            )

        assert changed.draft_revision == 1
        assert changed.snapshot["changes"] == ["Tighter opening", "Product reveal last"]
        assert changed.can_undo is True

        async with AsyncSessionLocal() as db:
            with pytest.raises(RuntimeFailure) as stale:
                await write_draft(
                    db,
                    thread_id=thread_id,
                    creator_id=user_id,
                    expected_revision=initial.draft_revision,
                    expected_etag=initial.etag,
                    snapshot=changed_snapshot,
                )
        assert getattr(stale.value, "code", None) == "draft_stale"

        async with AsyncSessionLocal() as db:
            restored, successor_id = await undo_draft(
                db,
                thread_id=thread_id,
                creator_id=user_id,
                expected_revision=changed.draft_revision,
            )

        assert successor_id is None
        assert restored.draft_revision == 2
        assert restored.snapshot == initial.snapshot
        assert restored.can_undo is True

        with sync_session() as db:
            heads = list(
                db.execute(
                    select(CreatorEditDraft).where(
                        CreatorEditDraft.thread_id == thread_id,
                        CreatorEditDraft.is_head.is_(True),
                    )
                ).scalars()
            )
            assert [row.id for row in heads] == [uuid.UUID(restored.draft_id)]
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_draft_retention_prunes_only_old_superseded_bodies() -> None:
    user_id, thread_id, _session_id = _seed_runtime_project()
    try:
        async with AsyncSessionLocal() as db:
            initial = await read_or_bootstrap_draft(db, thread_id=thread_id, creator_id=user_id)
            changed = await write_draft(
                db,
                thread_id=thread_id,
                creator_id=user_id,
                expected_revision=initial.draft_revision,
                expected_etag=initial.etag,
                snapshot={**initial.snapshot, "intent": "Keep the product reveal last."},
            )
        with sync_session() as db:
            old = db.get(CreatorEditDraft, uuid.UUID(initial.draft_id), with_for_update=True)
            assert old is not None
            old.created_at = datetime.now(UTC) - timedelta(days=31)
            db.commit()

        result = await asyncio.to_thread(prune_kria_drafts.run)
        assert result["pruned"] >= 1

        with sync_session() as db:
            old = db.get(CreatorEditDraft, uuid.UUID(initial.draft_id))
            head = db.get(CreatorEditDraft, uuid.UUID(changed.draft_id))
            assert old is not None and old.snapshot_json is None
            assert head is not None and head.snapshot_json is not None and head.is_head
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("request_render", "followup"),
    [
        (True, None),
        (True, "saved_after_approval"),
        (False, "style"),
        (False, "stale_head"),
        (False, "speech"),
    ],
)
async def test_editor_revision_approval_atomically_stages_exact_job_generation(
    monkeypatch: pytest.MonkeyPatch,
    request_render: bool,
    followup: str | None,
) -> None:
    user_id, thread_id, session_id = _seed_runtime_project()
    job_id = uuid.uuid4()
    with sync_session() as db:
        session = db.get(CreatorAgentSession, session_id, with_for_update=True)
        item = db.get(PlanItem, session.plan_item_id, with_for_update=True)
        job = Job(
            id=job_id,
            user_id=user_id,
            status="variants_ready",
            mode="generative",
            raw_storage_path="",
            selected_platforms=["tiktok"],
            content_plan_item_id=item.id,
            content_plan_ownership_epoch=0,
            all_candidates={"clip_paths": ["users/test/matcha.mp4"]},
            assembly_plan={
                "variants": [
                    {
                        "variant_id": "original_text",
                        "resolved_archetype": "montage",
                        "render_status": "ready",
                        "render_generation_id": "generation-1",
                        "render_finished_at": "2026-09-07T08:00:00Z",
                        "video_path": "generative-jobs/test/output.mp4",
                        "base_video_path": "generative-jobs/test/base.mp4",
                        "text_elements": [
                            {
                                "id": "hook",
                                "text": "Old matcha hook",
                                "start_s": 0.0,
                                "end_s": 2.0,
                                "role": "generative_intro",
                                "font_family": "Playfair Display",
                                "size_px": 72,
                                "color": "#FFFFFF",
                                "effect": "static",
                                "alignment": "center",
                                "position": "middle",
                            }
                        ],
                    }
                ]
            },
        )
        db.add(job)
        db.flush()
        item.current_job_id = job.id
        session.target_job_id = job.id
        session.target_variant_id = "original_text"
        # Reproduce the editor-Save gap: the exact variant is current, while
        # the session pointer still names the prior generation. The committed
        # editor turn must resync the pointer before minting any approval.
        session.target_generation_id = "stale-generation"
        session.manifest_hash = "a" * 64
        db.commit()

    body = SubmitTurnBody(
        message="Change the hook to Fresh matcha, finally",
        client_event_id=f"editor-{uuid.uuid4().hex}",
        expected_thread_revision=2,
    )
    editor_plan = adapt_editor_action(
        reply="I prepared a sharper product-first hook.",
        request_render=request_render,
        ops=[{"op": "edit_text", "bar_index": 0, "text": "Fresh matcha, finally"}],
    )

    async def _planned(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        return PlannedKriaTurn(
            plan=editor_plan,
            manifest_hash="a" * 64,
            context_hash="b" * 64,
        )

    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", _planned)
    monkeypatch.setattr(
        "app.tasks.kria_runtime.enqueue_editor_commit_render", lambda *_a, **_k: None
    )

    try:
        async with AsyncSessionLocal() as db:
            accepted, _ = await submit_turn(
                db,
                thread_id=thread_id,
                creator_id=user_id,
                body=body,
            )
        result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
        if not request_render:
            assert result["status"] == "completed"
            with sync_session() as db:
                assert (
                    db.get(CreatorAgentSession, session_id).target_generation_id == "generation-1"
                )
                turn = db.get(CreatorAgentTurn, uuid.UUID(accepted.turn_id))
                assert (
                    db.execute(
                        select(CreatorAgentApproval).where(CreatorAgentApproval.turn_id == turn.id)
                    ).scalar_one_or_none()
                    is None
                )
                draft = db.execute(
                    select(CreatorEditDraft).where(
                        CreatorEditDraft.thread_id == thread_id, CreatorEditDraft.is_head.is_(True)
                    )
                ).scalar_one()
                assert (
                    draft.snapshot_json["editor_payload"]["text_elements"][0]["text"]
                    == "Fresh matcha, finally"
                )
                unchanged = db.get(Job, job_id).assembly_plan["variants"][0]
                assert unchanged["render_generation_id"] == "generation-1"
                assert unchanged["render_status"] == "ready"
                assert unchanged["text_elements"][0]["text"] == "Old matcha hook"
                first_draft_id = draft.id
                item_id = draft.item_id
                first_revision = draft.draft_revision
                if followup == "stale_head":
                    # A surviving head from an older render must not become input
                    # to either the planner or the next committed draft.
                    # Freshness is the payload's own base_generation (the column
                    # and session pointer go stale after an editor Save).
                    snapshot = copy.deepcopy(draft.snapshot_json)
                    snapshot["editor_payload"]["base_generation"] = "older-generation"
                    draft.snapshot_json = snapshot
                    # Real prod shape: column + session pointer stale together.
                    draft.base_generation_id = "older-generation"
                    db.get(
                        CreatorAgentSession, session_id
                    ).target_generation_id = "older-generation"
                    db.commit()

            from types import SimpleNamespace

            from app.kria.planner import _plan_editor_revision

            observed_snapshots = []

            async def copilot_reply(body, **_kwargs):  # noqa: ANN001, ANN003, ANN202
                observed_snapshots.append(body.snapshot)
                return SimpleNamespace(ops=[], outcome="clarification", reply="Which style?")

            monkeypatch.setattr("app.kria.planner.run_copilot_turn", copilot_reply)
            async with AsyncSessionLocal() as db:
                item = await db.get(PlanItem, item_id)
                await _plan_editor_revision(
                    db, thread_id=thread_id, item=item, user_message="Make that hook red"
                )
            expected_text = (
                "Old matcha hook" if followup == "stale_head" else "Fresh matcha, finally"
            )
            assert observed_snapshots[0]["text_bars"][0]["text"] == expected_text

            editor_plan = adapt_editor_action(
                reply="Apply the next edit.",
                request_render=followup == "speech",
                ops=(
                    [{"op": "apply_speech_cut_candidate", "candidate_id": "reviewed-cut"}]
                    if followup == "speech"
                    else [{"op": "patch_text_style", "bar_index": 0, "patch": {"color": "#FF0000"}}]
                ),
            )
            with sync_session() as db:
                revision = db.get(CreationThread, thread_id).revision
            async with AsyncSessionLocal() as db:
                second, _ = await submit_turn(
                    db,
                    thread_id=thread_id,
                    creator_id=user_id,
                    body=SubmitTurnBody(
                        message="Apply the next edit",
                        client_event_id=f"followup-{uuid.uuid4().hex}",
                        expected_thread_revision=revision,
                    ),
                )
            if followup == "speech":
                with pytest.raises(RuntimeError, match="Save the current draft before"):
                    await asyncio.to_thread(run_kria_turn.run, second.turn_id)
            else:
                second_result = await asyncio.to_thread(run_kria_turn.run, second.turn_id)
                assert second_result["status"] == "completed"
            with sync_session() as db:
                heads = (
                    db.execute(
                        select(CreatorEditDraft).where(
                            CreatorEditDraft.thread_id == thread_id,
                            CreatorEditDraft.is_head.is_(True),
                        )
                    )
                    .scalars()
                    .all()
                )
                assert len(heads) == 1
                head = heads[0]
                if followup == "speech":
                    assert head.id == first_draft_id
                    assert head.draft_revision == first_revision
                    assert db.get(CreatorAgentTurn, uuid.UUID(second.turn_id)).status == "failed"
                else:
                    assert head.id != first_draft_id
                    assert head.draft_revision == first_revision + 1
                    assert not db.get(CreatorEditDraft, first_draft_id).is_head
                    bar = head.snapshot_json["editor_payload"]["text_elements"][0]
                    assert bar["text"] == expected_text
                    assert bar["color"] == "#FF0000"
                    assert head.base_generation_id == "generation-1"
                assert (
                    db.execute(
                        select(CreatorAgentApproval).where(
                            CreatorAgentApproval.turn_id == uuid.UUID(second.turn_id)
                        )
                    ).scalar_one_or_none()
                    is None
                )
                unchanged = db.get(Job, job_id).assembly_plan["variants"][0]
                assert unchanged["text_elements"][0]["text"] == "Old matcha hook"
                assert unchanged["render_generation_id"] == "generation-1"
                assert unchanged["render_status"] == "ready"
            return
        assert result["status"] == "awaiting_approval"

        with sync_session() as db:
            assert db.get(CreatorAgentSession, session_id).target_generation_id == "generation-1"
            turn = db.get(CreatorAgentTurn, uuid.UUID(accepted.turn_id))
            approval = db.execute(
                select(CreatorAgentApproval).where(CreatorAgentApproval.turn_id == turn.id)
            ).scalar_one()
            assert approval.target_job_id == job_id
            assert approval.target_variant_id == "original_text"
            assert approval.target_generation_id == "generation-1"
            draft = db.get(CreatorEditDraft, approval.draft_id)
            assert draft.snapshot_json["kind"] == "editor"
            assert draft.snapshot_json["editor_payload"]["base_generation"] == "generation-1"
            fingerprint = approval_fingerprint(approval)
            thread_revision = db.get(CreationThread, thread_id).revision

        async with AsyncSessionLocal() as db:
            await decide_approval(
                db,
                thread_id=thread_id,
                approval_id=approval.id,
                creator_id=user_id,
                decision="approve",
                body=ApprovalDecisionBody(
                    expected_thread_revision=thread_revision,
                    expected_draft_revision=approval.draft_revision,
                    expected_approval_fingerprint=fingerprint,
                ),
            )

        if followup == "saved_after_approval":
            # A manual editor Save can publish a newer exact variant after
            # approval creation while the session pointer still names the
            # approved generation. Claiming the old approval must cancel it
            # without accepting or rewriting the newer saved generation.
            with sync_session() as db:
                assert db.get(CreatorAgentSession, session_id).target_generation_id == (
                    "generation-1"
                )
                job = db.get(Job, job_id, with_for_update=True)
                variants = [dict(row) for row in job.assembly_plan["variants"]]
                variants[0]["render_generation_id"] = "newer-save"
                job.assembly_plan = {**job.assembly_plan, "variants": variants}
                db.commit()

            first_claim = await asyncio.to_thread(_claim_approval_dispatch, approval.id)
            assert first_claim is None
            with sync_session() as db:
                saved_job = db.get(Job, job_id)
                saved_variant = saved_job.assembly_plan["variants"][0]
                saved_approval = db.get(CreatorAgentApproval, approval.id)
                saved_execution = db.get(
                    CreatorAgentExecution,
                    uuid.UUID(saved_approval.execution_ids[0]),
                )
                assert saved_variant["render_generation_id"] == "newer-save"
                assert saved_approval.status == "cancelled"
                assert saved_execution.status == "stale"
                assert saved_execution.target_generation_id == "generation-1"
            return

        # Simulate a process dying after approval consumption + Job commit but
        # before broker publication. The next task invocation must recover the
        # accepted generation rather than compiling or staging it again.
        first_claim = await asyncio.to_thread(_claim_approval_dispatch, approval.id)
        assert first_claim is not None
        assert first_claim.target_generation_id != "generation-1"
        staged_generation = first_claim.target_generation_id

        dispatched = await asyncio.to_thread(execute_kria_approval.run, str(approval.id))
        assert dispatched == {
            "approval_id": str(approval.id),
            "status": "dispatched",
            "job_id": str(job_id),
        }

        with sync_session() as db:
            job = db.get(Job, job_id)
            variant = job.assembly_plan["variants"][0]
            assert variant["text_elements"][0]["text"] == "Fresh matcha, finally"
            assert variant["render_status"] == "rendering"
            assert variant["render_generation_id"] == staged_generation
            approval = db.get(CreatorAgentApproval, approval.id)
            execution = db.get(
                CreatorAgentExecution,
                uuid.UUID(approval.execution_ids[0]),
            )
            assert approval.status == "consumed"
            assert execution.status == "dispatched"
            assert execution.target_job_id == job_id
            assert execution.target_generation_id == variant["render_generation_id"]
            execution_id = execution.id

            # A terminal sibling must not satisfy this receipt while the exact
            # editor generation remains in flight on the reused Job.
            job.assembly_plan = {
                **job.assembly_plan,
                "variants": [
                    *job.assembly_plan["variants"],
                    {
                        "variant_id": "song_text",
                        "render_status": "ready",
                        "render_generation_id": "sibling-generation",
                        "output_url": "https://example.test/sibling.mp4",
                    },
                ],
            }
            db.commit()

        observed, promoted = await asyncio.to_thread(
            _observe_dispatched_execution,
            execution_id,
        )
        assert observed == "pending"
        assert promoted is None
        with sync_session() as db:
            execution = db.get(CreatorAgentExecution, execution_id)
            assert execution.status == "dispatched"
            assert execution.target_variant_id == "original_text"
            assert execution.target_generation_id == staged_generation
            job = db.get(Job, job_id, with_for_update=True)
            variants = [dict(row) for row in job.assembly_plan["variants"]]
            variants[0]["render_status"] = "ready"
            variants[0]["output_url"] = "https://example.test/exact.mp4"
            job.assembly_plan = {**job.assembly_plan, "variants": variants}
            db.commit()

        observed, promoted = await asyncio.to_thread(
            _observe_dispatched_execution,
            execution_id,
        )
        assert observed == "completed"
        assert promoted is None
        with sync_session() as db:
            execution = db.get(CreatorAgentExecution, execution_id)
            assert execution.status == "completed"
            assert execution.result["variant_id"] == "original_text"
            assert execution.result["render_generation_id"] == staged_generation
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("manual_rename", [False, True])
async def test_first_prompt_title_is_durable_and_manual_rename_wins(
    monkeypatch: pytest.MonkeyPatch, manual_rename: bool
) -> None:
    from app.services import creation_thread_titles

    user_id, thread_id, _session_id = _seed_runtime_project()
    prompt = "Create a 30-second reel from my Barcelona trip clips"
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        thread.title = "Untitled video"
        db.commit()

    started, finish = asyncio.Event(), asyncio.Event()
    calls = []

    async def summarize(text: str) -> str:
        calls.append(text)
        started.set()
        await finish.wait()
        return "Barcelona Trip Reel"

    monkeypatch.setattr(creation_thread_titles, "_summarize", summarize)
    task = None
    try:
        async with AsyncSessionLocal() as db:
            accepted, _ = await submit_turn(
                db,
                thread_id=thread_id,
                creator_id=user_id,
                body=SubmitTurnBody(
                    message=prompt,
                    client_event_id=f"title-{uuid.uuid4()}",
                    expected_thread_revision=2,
                ),
            )
        task = asyncio.create_task(creation_thread_titles.generate_thread_title(thread_id))
        await asyncio.wait_for(started.wait(), timeout=2)
        # A second API process observes the durable claim and performs no paid call.
        await creation_thread_titles.generate_thread_title(thread_id)
        if manual_rename:
            async with AsyncSessionLocal() as db:
                thread = await db.get(CreationThread, thread_id, with_for_update=True)
                # Even explicitly choosing the fallback text establishes ownership.
                thread.title = prompt
                thread.state = {**thread.state, "title_source": "user"}
                await db.commit()
        finish.set()
        await task
        async with AsyncSessionLocal() as db:
            reopened = await db.get(CreationThread, thread_id)
            assert reopened.title == (prompt if manual_rename else "Barcelona Trip Reel")
            assert reopened.state["title_source"] == ("user" if manual_rename else "generated")
            delta = await read_delta(
                db,
                thread_id=thread_id,
                creator_id=user_id,
                after_sequence=2,
                limit=20,
            )
            assert [event.event_type for event in delta.events] == (
                [] if manual_rename else ["thread_title_generated"]
            )
        # A follow-up accepted against the pre-title revision is still valid.
        async with AsyncSessionLocal() as db:
            await submit_turn(
                db,
                thread_id=thread_id,
                creator_id=user_id,
                body=SubmitTurnBody(
                    message="Keep the sunset shot",
                    client_event_id=f"followup-{uuid.uuid4()}",
                    expected_thread_revision=accepted.thread_revision,
                ),
            )
        await creation_thread_titles.generate_thread_title(thread_id)
        assert calls == [prompt]
    finally:
        finish.set()
        if task is not None:
            await task
        await async_engine.dispose()


# ---------------------------------------------------------------------------
# KRI-188: Creative Brief persistence, receipts and dispatch request (real DB)
# ---------------------------------------------------------------------------


def _brief_strategy_plan() -> KriaTurnPlan:
    from app.schemas.clip_intents import ClipAssignment, ResolvedClipIntent

    labels = ResolvedClipIntent(
        intent_id="landmark",
        op="label",
        attribute="the landmark shown",
        assignments=[
            ClipAssignment(
                media_id="clip-1",
                value="Eminonu",
                confidence=0.9,
                evidence="Bridge visible",
                grounding="vision_verified",
            )
        ],
    )
    return adapt_creator_action(
        ProposeStrategy(
            kind="propose_strategy",
            strategy=CreativeStrategy(
                direction="guided_story",
                edit_format="day_vlog",
                audio_strategy="licensed_music",
                pacing="fast",
                render_program="guided",
                selected_media_ids=[],
                rationale="Chronological run.",
                opening_title="20K Kosu",
                target_duration_s=20,
            ),
            summary="A chronological 20K cut.",
        ),
        server_clip_intents=[labels],
        server_resolved_clip_intents=[labels],
    )


def _brief_updates() -> tuple[BriefUpdate, ...]:
    return (
        BriefUpdate(kind="text", scope="title", literal="20K Kosu"),
        BriefUpdate(kind="text", scope="per_clip", description="the landmark in each clip"),
    )


async def _submit(user_id, thread_id, message, revision):  # noqa: ANN001, ANN202
    async with AsyncSessionLocal() as db:
        accepted, _ = await submit_turn(
            db,
            thread_id=thread_id,
            creator_id=user_id,
            body=SubmitTurnBody(
                message=message,
                client_event_id=f"brief-{uuid.uuid4().hex}",
                expected_thread_revision=revision,
            ),
        )
    return accepted


@pytest.mark.asyncio
@pytest.mark.parametrize("brief_on", [True, False])
async def test_brief_turn_persists_version_receipts_and_dispatch_request(
    monkeypatch: pytest.MonkeyPatch, brief_on: bool
) -> None:
    user_id, thread_id, session_id = _seed_runtime_project()
    plan = _brief_strategy_plan()

    async def _planned(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        extra = (
            {
                "brief_updates": _brief_updates(),
                "brief_route": "replan",
                "brief_clip_ids": ("clip-1", "clip-2"),
            }
            if brief_on
            else {}
        )
        return PlannedKriaTurn(plan=plan, manifest_hash="a" * 64, context_hash="b" * 64, **extra)

    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", brief_on)
    monkeypatch.setattr(settings, "kria_creative_brief_user_ids", [])
    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", _planned)
    try:
        accepted = await _submit(
            user_id, thread_id, "Title it 20K Kosu and label each clip with its landmark", 2
        )
        result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
        assert result["status"] == "awaiting_approval"

        with sync_session() as db:
            versions = list(
                db.execute(
                    select(CreativeBriefVersion).where(CreativeBriefVersion.thread_id == thread_id)
                ).scalars()
            )
            draft_event = db.execute(
                select(CreationThreadEvent).where(
                    CreationThreadEvent.thread_id == thread_id,
                    CreationThreadEvent.event_type == "draft_applied",
                )
            ).scalar_one()
            payload = dict(draft_event.payload)
            content = draft_event.content
            approval = db.execute(
                select(CreatorAgentApproval).where(
                    CreatorAgentApproval.turn_id == uuid.UUID(accepted.turn_id)
                )
            ).scalar_one()
            approval_id = approval.id
            draft_revision = approval.draft_revision
            approval_token = approval_fingerprint(approval)
            thread_revision = db.get(CreationThread, thread_id).revision
            item_id = db.get(CreatorAgentSession, session_id).plan_item_id
            version_rows = [(v.version, v.requirements, v.source_turn_id) for v in versions]

        if not brief_on:
            # Flag off: byte-identical to the pre-feature reply and payload.
            assert version_rows == []
            assert content == "A chronological 20K cut."
            assert "requirement_receipts" not in payload
        else:
            assert [row[0] for row in version_rows] == [1]
            assert [r["kind"] for r in version_rows[0][1]] == ["text", "text"]
            assert version_rows[0][2] == uuid.UUID(accepted.turn_id)
            receipts = {r["requirement_id"]: r for r in payload["requirement_receipts"]}
            assert receipts["r1"]["status"] == "met"  # literal title is in the strategy
            # 1 of the 2 clips got a label, and it was inferred from the footage.
            assert receipts["r2"]["status"] == "partial"
            assert receipts["r2"]["inferred"] == ["Eminonu"]
            assert content.startswith("Not everything you asked for made it in")
            assert "1 of 2" in content and "Eminonu" in content
            assert "A chronological 20K cut." not in content

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
                    expected_approval_fingerprint=approval_token,
                ),
            )
        seen: dict = {}

        def _fake_dispatch(*_args, **kwargs) -> DispatchResult:  # noqa: ANN002, ANN003
            seen.update(kwargs)
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

        monkeypatch.setattr("app.tasks.content_plan_build.dispatch_item_render_for", _fake_dispatch)
        await asyncio.to_thread(execute_kria_approval.run, str(approval_id))
        if brief_on:
            # The strategy's creator request is the brief, not the draft summary.
            assert seen["creator_request"].startswith("Creative brief")
            assert '"20K Kosu"' in seen["creator_request"]
            assert "the landmark in each clip" in seen["creator_request"]
        else:
            assert seen["creator_request"] == "A chronological 20K cut."
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_brief_version_is_idempotent_per_turn_and_never_written_on_requeue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.kria.brief import persist_brief_version_sync
    from app.tasks.kria_runtime import _claim, _complete_response_turn

    user_id, thread_id, _session_id = _seed_runtime_project()
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", True)
    try:
        accepted = await _submit(user_id, thread_id, "Order them by when I filmed them", 2)
        turn_id = uuid.UUID(accepted.turn_id)
        claimed = await asyncio.to_thread(_claim, turn_id, "owner-1")
        assert claimed is not None
        _snapshot, _message, lease_epoch, revision, _editor_state = claimed
        question = KriaTurnPlan(mode="respond", turn_value="question", response="Which order?")
        updates = (BriefUpdate(kind="order", scope="global", description="chronological"),)

        # A stale thread revision requeues the turn and must not record a version.
        stale = await asyncio.to_thread(
            lambda: _complete_response_turn(
                turn_id,
                lease_owner="owner-1",
                lease_epoch=lease_epoch,
                claimed_thread_revision=revision - 1,
                plan=question,
                brief_updates=updates,
            )
        )
        assert stale.committed is False and stale.requeue_turn_id == str(turn_id)
        with sync_session() as db:
            count = db.scalar(
                select(func.count())
                .select_from(CreativeBriefVersion)
                .where(CreativeBriefVersion.thread_id == thread_id)
            )
            assert count == 0

        claimed = await asyncio.to_thread(_claim, turn_id, "owner-2")
        assert claimed is not None
        _snapshot, _message, lease_epoch, revision, _editor_state = claimed
        done = await asyncio.to_thread(
            lambda: _complete_response_turn(
                turn_id,
                lease_owner="owner-2",
                lease_epoch=lease_epoch,
                claimed_thread_revision=revision,
                plan=question,
                brief_updates=updates,
                requirement_receipts=[
                    {
                        "requirement_id": "r1",
                        "status": "partial",
                        "verification": "unchecked",
                        "stage": "understood",
                        "reason": "Needs an output check.",
                    }
                ],
            )
        )
        assert done.committed is True

        def _persist_again() -> int:
            with sync_session() as db:
                brief = persist_brief_version_sync(
                    db, thread_id=thread_id, turn_id=turn_id, updates=updates
                )
                db.commit()
                return brief.version

        assert await asyncio.to_thread(_persist_again) == 1
        with sync_session() as db:
            rows = list(
                db.execute(
                    select(CreativeBriefVersion.version).where(
                        CreativeBriefVersion.thread_id == thread_id
                    )
                ).scalars()
            )
            assert rows == [1]
            event = db.execute(
                select(CreationThreadEvent).where(
                    CreationThreadEvent.thread_id == thread_id,
                    CreationThreadEvent.event_type == "assistant_response",
                )
            ).scalar_one()
            receipt = event.payload["requirement_receipts"][0]
            assert receipt["brief_version"] == 1
            assert receipt["generation_id"] is None
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_brief_read_service_returns_requirements_and_newest_receipts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.kria.runtime import read_creative_brief

    user_id, thread_id, _session_id = _seed_runtime_project()

    async def _planned(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        return PlannedKriaTurn(
            plan=_brief_strategy_plan(),
            manifest_hash="a" * 64,
            context_hash="b" * 64,
            brief_updates=_brief_updates(),
            brief_route="replan",
            brief_clip_ids=("clip-1", "clip-2"),
        )

    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", True)
    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", _planned)
    try:
        accepted = await _submit(user_id, thread_id, "Title it 20K Kosu", 2)
        await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
        async with AsyncSessionLocal() as db:
            brief = await read_creative_brief(db, thread_id=thread_id, creator_id=user_id)
        assert brief.version == 1
        assert {(r.id, r.status) for r in brief.requirements} == {("r1", "met"), ("r2", "partial")}
        assert {r.requirement_id for r in brief.requirement_receipts} == {"r1", "r2"}
        monkeypatch.setattr(settings, "kria_creative_brief_enabled", False)
        async with AsyncSessionLocal() as db:
            off = await read_creative_brief(db, thread_id=thread_id, creator_id=user_id)
        assert off.version == 0 and off.requirements == []
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_brief_read_skips_stored_receipts_that_judged_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Events written before unjudged receipts were dropped still hold "can't verify"
    results. They never become a requirement's outcome: "add captions" stays open,
    and an older judged receipt behind a newer unjudged one still counts."""
    from app.kria.brief import BriefRequirement
    from app.kria.runtime import read_creative_brief

    user_id, thread_id, _session_id = _seed_runtime_project()
    requirements = [
        BriefRequirement(id="r1", kind="text", scope="title", literal="Hi"),
        BriefRequirement(id="r2", kind="style", scope="global", description="add captions"),
        BriefRequirement(
            id="r3", kind="style", scope="global", description="pop up X when I say Y"
        ),
    ]
    cant_verify = "I can't verify this one automatically yet."
    older = [{"requirement_id": "r3", "status": "met", "reason": None, "inferred": []}]
    newer = [
        {"requirement_id": "r1", "status": "met", "reason": None, "inferred": []},
        {"requirement_id": "r2", "status": "partial", "reason": cant_verify, "inferred": []},
        {
            "requirement_id": "r3",
            "status": "partial",
            "reason": "I can't check the pop-ins on this draft yet.",
            "inferred": [],
        },
    ]
    with sync_session() as db:
        db.add(
            CreativeBriefVersion(
                thread_id=thread_id,
                version=1,
                requirements=[req.model_dump(mode="json") for req in requirements],
                source_turn_id=None,
            )
        )
        thread = db.get(CreationThread, thread_id)
        assert thread is not None
        thread.revision = 4
        for sequence, receipts in ((2, older), (3, newer)):
            db.add(
                CreationThreadEvent(
                    thread_id=thread_id,
                    sequence=sequence,
                    revision=sequence + 1,
                    role="assistant",
                    event_type="draft_applied",
                    content="Drafted.",
                    payload={"requirement_receipts": receipts},
                )
            )
        db.commit()

    monkeypatch.setattr(settings, "kria_creative_brief_enabled", True)
    try:
        async with AsyncSessionLocal() as db:
            brief = await read_creative_brief(db, thread_id=thread_id, creator_id=user_id)
        assert {(r.id, r.status) for r in brief.requirements} == {
            ("r1", "met"),
            ("r2", "open"),
            ("r3", "met"),
        }
        assert sorted(r.requirement_id for r in brief.requirement_receipts) == ["r1", "r3"]
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_brief_read_never_recredits_unversioned_receipt_after_stable_id_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A flag rollback cannot apply a legacy success to revised wording."""
    from app.kria.brief import BriefRequirement
    from app.kria.runtime import read_creative_brief

    user_id, thread_id, _session_id = _seed_runtime_project()
    old = BriefRequirement(id="r1", kind="text", scope="title", literal="Old title")
    changed = BriefRequirement(id="r1", kind="text", scope="title", literal="New title")
    with sync_session() as db:
        db.add_all(
            [
                CreativeBriefVersion(
                    thread_id=thread_id,
                    version=1,
                    requirements=[old.model_dump(mode="json")],
                    source_turn_id=None,
                ),
                CreativeBriefVersion(
                    thread_id=thread_id,
                    version=2,
                    requirements=[changed.model_dump(mode="json")],
                    source_turn_id=None,
                ),
            ]
        )
        db.add(
            CreationThreadEvent(
                thread_id=thread_id,
                sequence=2,
                revision=3,
                role="assistant",
                event_type="draft_applied",
                content="Drafted.",
                payload={
                    "requirement_receipts": [
                        {"requirement_id": "r1", "status": "met", "inferred": []}
                    ]
                },
            )
        )
        db.commit()

    monkeypatch.setattr(settings, "kria_creative_brief_enabled", True)
    try:
        async with AsyncSessionLocal() as db:
            brief = await read_creative_brief(db, thread_id=thread_id, creator_id=user_id)
        assert [(row.id, row.status) for row in brief.requirements] == [("r1", "open")]
        assert brief.requirement_receipts == []
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_editor_ops_turn_receipts_cover_only_this_turns_requirements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KRI-188: editor-op receipts are scoped to this turn and never say "Couldn't"
    for a requirement the editor payload simply has no structure to verify."""
    from app.kria.brief import BriefRequirement

    user_id, thread_id, session_id = _seed_runtime_project()
    job_id = uuid.uuid4()
    with sync_session() as db:
        session = db.get(CreatorAgentSession, session_id, with_for_update=True)
        item = db.get(PlanItem, session.plan_item_id, with_for_update=True)
        db.add(
            Job(
                id=job_id,
                user_id=user_id,
                status="variants_ready",
                mode="generative",
                raw_storage_path="",
                selected_platforms=["tiktok"],
                content_plan_item_id=item.id,
                content_plan_ownership_epoch=0,
                all_candidates={"clip_paths": ["users/test/matcha.mp4"]},
                assembly_plan={
                    "variants": [
                        {
                            "variant_id": "original_text",
                            "resolved_archetype": "montage",
                            "render_status": "ready",
                            "render_generation_id": "generation-1",
                            "render_finished_at": "2026-09-07T08:00:00Z",
                            "video_path": "generative-jobs/test/output.mp4",
                            "base_video_path": "generative-jobs/test/base.mp4",
                            "text_elements": [
                                {
                                    "id": "hook",
                                    "text": "Old matcha hook",
                                    "start_s": 0.0,
                                    "end_s": 2.0,
                                    "role": "generative_intro",
                                    "font_family": "Playfair Display",
                                    "size_px": 72,
                                    "color": "#FFFFFF",
                                    "effect": "static",
                                    "alignment": "center",
                                    "position": "middle",
                                }
                            ],
                        }
                    ]
                },
            )
        )
        db.flush()
        item.current_job_id = job_id
        session.target_job_id = job_id
        session.target_variant_id = "original_text"
        session.target_generation_id = "generation-1"
        session.manifest_hash = "a" * 64
        # An older requirement from a previous turn: must NOT be receipted here.
        old = BriefRequirement(
            id="r1",
            kind="order",
            scope="global",
            description="chronological",
            facts={"key": "capture_time"},
            source_turn_id=None,
        )
        db.add(
            CreativeBriefVersion(
                thread_id=thread_id,
                version=1,
                requirements=[old.model_dump(mode="json")],
                source_turn_id=None,
            )
        )
        db.commit()

    editor_plan = adapt_editor_action(
        reply="Retitled the hook.",
        request_render=False,
        ops=[{"op": "edit_text", "bar_index": 0, "text": "Fresh matcha, finally"}],
    )

    async def _planned(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        return PlannedKriaTurn(
            plan=editor_plan,
            manifest_hash="a" * 64,
            context_hash="b" * 64,
            brief_updates=(
                BriefUpdate(kind="text", scope="title", literal="Fresh matcha, finally"),
                BriefUpdate(kind="text", scope="per_clip", description="a label on each clip"),
            ),
            brief_route="editor_ops",
            brief_clip_ids=("clip-1", "clip-2"),
        )

    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", True)
    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", _planned)
    try:
        accepted = await _submit(user_id, thread_id, "Change the hook to Fresh matcha, finally", 2)
        result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
        assert result["status"] == "completed"
        with sync_session() as db:
            event = db.execute(
                select(CreationThreadEvent).where(
                    CreationThreadEvent.thread_id == thread_id,
                    CreationThreadEvent.event_type == "draft_applied",
                )
            ).scalar_one()
            receipts = {r["requirement_id"]: r for r in event.payload["requirement_receipts"]}
            content = event.content
        # r1 is from an earlier turn; r3 (per-clip text with no exact words) can't
        # be checked on an editor edit, so it gets no receipt rather than "Partly".
        assert set(receipts) == {"r2"}
        assert receipts["r2"]["status"] == "met"
        assert "Couldn't" not in content and "Partly" not in content
        assert content == 'Retitled the hook.\n- Done: "Fresh matcha, finally"'
    finally:
        await async_engine.dispose()


def _pinned_rows() -> list[dict]:
    """The two bottom-left pinned texts of the Lisbon thread (KRI-529), stacked."""
    return [
        {
            "id": f"guided-pinned-{index}",
            "text": text,
            "start_s": 0.0,
            "end_s": 9.0,
            "role": "generative_intro",
            "font_family": "Playfair Display",
            "size_px": 52,
            "color": "#FFFFFF",
            "effect": "static",
            "position": "custom",
            "alignment": "left",
            "x_frac": 0.0856,
            "y_frac": y,
        }
        for index, (text, y) in enumerate((("Must visit spots in Lisbon", 0.84), ("Part 1", 0.91)))
    ]


@pytest.mark.asyncio
async def test_kri529_lisbon_editor_turns_scope_receipts_and_keep_each_texts_height(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KRI-529, replayed through the real worker with the brief binding ON (the founder's
    cohort): an untouched earlier requirement gets no "still needs an output check" chip,
    a style ask is receipted alone, and "left-align them together" moves only x."""
    from app.kria.brief import BriefRequirement

    user_id, thread_id, session_id = _seed_runtime_project()
    job_id = uuid.uuid4()
    pinned = [row["id"] for row in _pinned_rows()]
    with sync_session() as db:
        session = db.get(CreatorAgentSession, session_id, with_for_update=True)
        item = db.get(PlanItem, session.plan_item_id, with_for_update=True)
        db.add(
            Job(
                id=job_id,
                user_id=user_id,
                status="variants_ready",
                mode="generative",
                raw_storage_path="",
                selected_platforms=["tiktok"],
                content_plan_item_id=item.id,
                content_plan_ownership_epoch=0,
                all_candidates={"clip_paths": ["users/test/lisbon.mp4"]},
                assembly_plan={
                    "variants": [
                        {
                            "variant_id": "original_text",
                            "resolved_archetype": "montage",
                            "render_status": "ready",
                            "render_generation_id": "generation-1",
                            "render_finished_at": "2026-10-08T08:00:00Z",
                            "video_path": "generative-jobs/test/output.mp4",
                            "base_video_path": "generative-jobs/test/base.mp4",
                            "text_elements": _pinned_rows(),
                        }
                    ]
                },
            )
        )
        db.flush()
        item.current_job_id = job_id
        session.target_job_id = job_id
        session.target_variant_id = "original_text"
        session.target_generation_id = "generation-1"
        session.manifest_hash = "a" * 64
        earlier = BriefRequirement(
            id="r1",
            kind="text",
            scope="global",
            description="The location name stays the whole video",
            source_turn_id=None,
        )
        db.add(
            CreativeBriefVersion(
                thread_id=thread_id,
                version=1,
                requirements=[earlier.model_dump(mode="json")],
                source_turn_id=None,
            )
        )
        db.commit()

    plans = iter(
        [
            (
                [
                    {
                        "op": "patch_text",
                        "selector": {"ids": pinned},
                        "target_ids": pinned,
                        "expected_count": len(pinned),
                        "patch": {"animation_phases": {"entrance": "fade"}},
                    }
                ],
                BriefUpdate(
                    kind="style", scope="global", description="Add fade-in animation to all of them"
                ),
            ),
            (
                [
                    {
                        "op": "patch_text",
                        "selector": {"ids": pinned},
                        "target_ids": pinned,
                        "expected_count": len(pinned),
                        # The model's x-only patch: it used to default y to the middle.
                        "patch": {"position": "custom", "alignment": "left", "x_frac": 0.08},
                    }
                ],
                BriefUpdate(
                    kind="style",
                    scope="global",
                    description="Left-align the two bottom-left texts together",
                ),
            ),
        ]
    )

    async def _planned(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        ops, update = next(plans)
        return PlannedKriaTurn(
            plan=adapt_editor_action(reply="Updated your edit.", request_render=False, ops=ops),
            manifest_hash="a" * 64,
            context_hash="b" * 64,
            brief_updates=(update,),
            brief_route="editor_ops",
            brief_clip_ids=("clip-1",),
        )

    async def _turn(message: str, editor_state=None):  # noqa: ANN001, ANN202
        async with AsyncSessionLocal() as db:
            revision = (
                await db.execute(
                    select(CreationThread.revision).where(CreationThread.id == thread_id)
                )
            ).scalar_one()
            accepted, _ = await submit_turn(
                db,
                thread_id=thread_id,
                creator_id=user_id,
                body=SubmitTurnBody(
                    message=message,
                    client_event_id=f"kri529-{uuid.uuid4().hex}",
                    expected_thread_revision=revision,
                    editor_state=editor_state,
                ),
            )
        result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
        assert result["status"] == "completed"
        with sync_session() as db:
            event = (
                db.execute(
                    select(CreationThreadEvent)
                    .where(
                        CreationThreadEvent.thread_id == thread_id,
                        CreationThreadEvent.event_type == "draft_applied",
                    )
                    .order_by(CreationThreadEvent.sequence.desc())
                )
                .scalars()
                .first()
            )
            return event.payload, event.content

    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", True)
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)
    monkeypatch.setattr(settings, "kria_editor_state_turns_enabled", True)
    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", _planned)
    try:
        payload, content = await _turn("Add fade-in animation to all of them")
        receipts = {r["requirement_id"]: r for r in payload["requirement_receipts"]}
        # r1 is an earlier requirement: no "still needs an output check" chip for it.
        assert set(receipts) == {"r2"}
        # KRI-524: a changed text lane does not prove a style ask, so it stays honestly
        # unchecked (it is not claimed "met"), but it is still just THIS turn's receipt.
        assert receipts["r2"]["verification"] == "unchecked"
        assert "output check" not in content

        # The creator's unsaved manual edit (a recolour) rides along as client state.
        manual = _pinned_rows()
        for row in manual:
            row["animation_phases"] = {"entrance": "fade", "exit": "none", "loop": "none"}
        manual[1]["color"] = "#FFD60A"
        payload, content = await _turn(
            "Left-align the two bottom-left texts together",
            editor_state={
                "base_generation": "generation-1",
                "client_state_id": "cs-lisbon-1",
                "lanes": {"text_elements": manual},
            },
        )
        receipts = {r["requirement_id"]: r for r in payload["requirement_receipts"]}
        assert set(receipts) == {"r3"}
        assert receipts["r3"]["verification"] == "unchecked"
        assert "output check" not in content
        with sync_session() as db:
            head = db.execute(
                select(CreatorEditDraft).where(
                    CreatorEditDraft.variant_key == "original_text",
                    CreatorEditDraft.is_head.is_(True),
                    CreatorEditDraft.item_id
                    == db.get(CreatorAgentSession, session_id).plan_item_id,
                )
            ).scalar_one()
            rows = {r["id"]: r for r in head.snapshot_json["editor_payload"]["text_elements"]}
        assert {rows[i]["x_frac"] for i in pinned} == {0.08}
        assert {rows[i]["alignment"] for i in pinned} == {"left"}
        assert [rows[i]["y_frac"] for i in pinned] == [0.84, 0.91]  # not dragged to 0.5
        assert rows[pinned[1]]["color"] == "#FFD60A"  # the manual edit survived the turn
        assert rows[pinned[0]]["animation_phases"]["entrance"] == "fade"
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "Add a title “Lisbon” under the text. Anymate typewrite to all texts but "
        "show lisbon after the current text finished animating",
        "Add a new title “Lisbon”. Animate it",
    ],
)
async def test_lisbon_followup_atomically_pins_current_request_without_recreation(
    monkeypatch: pytest.MonkeyPatch,
    message: str,
) -> None:
    """Sanitized four-clip replay crosses turn acceptance, draft commit, and binding."""
    from app.kria.brief import BriefRequirement, CreativeBrief
    from app.kria.brief_binding import BriefBinding
    from app.pipeline.guided_story import compile_execution_plan
    from app.pipeline.unified_montage import UnifiedClip, brief_view, plan_unified_montage
    from app.services.kria_editor_ops import build_editor_snapshot
    from tests.services.test_kria_editor_clip_context import _parse

    user_id, thread_id, session_id = _seed_runtime_project()
    job_id = uuid.uuid4()
    original = "Good Morning from the Erbens"
    brief = CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(id="r1", kind="text", scope="title", literal=original),
        ],
    )
    clips = [
        UnifiedClip(
            media_id=f"clip-{i}",
            proxy_path=f"users/test/morning-{i}.mp4",
            generation="1",
            duration_s=3.75,
            width=1080,
            height=1920,
        )
        for i in range(4)
    ]
    montage = plan_unified_montage(
        clips,
        brief_view(brief),
        strategy={
            "opening_title": original,
            "target_duration_s": 15,
        },
    )
    guided = montage.guided_edit()
    execution = compile_execution_plan(guided, track=None)
    variant = {
        "variant_id": "guided_story",
        "resolved_archetype": "guided_story",
        "render_status": "ready",
        "render_generation_id": "generation-1",
        "render_destination": "device",
        "text_elements": execution["text_elements"],
    }
    assembly = {
        "guided_edit": guided,
        "guided_story_execution_plan": execution,
        "variants": [variant],
    }
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", True)
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)
    monkeypatch.setattr(
        "app.services.kria_editor_ops._editor_capabilities",
        lambda *_: {"text_elements": True, "timeline": True},
    )
    with sync_session() as db:
        session = db.get(CreatorAgentSession, session_id)
        item = db.get(PlanItem, session.plan_item_id)
        job = Job(
            id=job_id,
            user_id=user_id,
            status="variants_ready",
            mode="generative",
            raw_storage_path="",
            selected_platforms=["tiktok"],
            content_plan_item_id=item.id,
            content_plan_ownership_epoch=0,
            assembly_plan=assembly,
            all_candidates={"clip_paths": [clip.proxy_path for clip in clips]},
        )
        db.add(job)
        db.flush()
        item.current_job_id = job_id
        session.target_job_id = job_id
        session.target_variant_id = "guided_story"
        session.target_generation_id = "generation-1"
        session.manifest_hash = "a" * 64
        db.add(
            CreativeBriefVersion(
                thread_id=thread_id,
                version=1,
                requirements=[r.model_dump(mode="json") for r in brief.requirements],
            )
        )
        snapshot = build_editor_snapshot(job, variant)
        db.commit()
    detailed = "typewrite" in message
    phase = "typewriter" if detailed else "pop"
    addition = {
        "op": "add_text",
        "text": "Lisbon",
        "start_s": 0,
        "end_s": 2,
        "style_from": "title",
        "animation_phases": {"entrance": phase},
    }
    if detailed:
        addition.update(below=True, after_animation_of="title")
    raw_ops = (
        [
            {
                "op": "patch_text",
                "selector": {"group": "all"},
                "patch": {"animation_phases": {"entrance": "typewriter"}},
            }
        ]
        if detailed
        else []
    ) + [addition]
    output = _parse(snapshot, raw_ops, message)
    assert output.ops, output.reply
    updates = [
        BriefUpdate(kind="text", scope="title", literal="Lisbon"),
        BriefUpdate(kind="style", scope="title", description=f"{phase} title animation"),
    ]
    if detailed:
        updates.append(
            BriefUpdate(
                kind="timing",
                scope="title",
                description="Show Lisbon after the current text finished animating",
            )
        )

    async def planned(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        return PlannedKriaTurn(
            plan=adapt_editor_action(
                reply="Review the Lisbon title edit.", request_render=False, ops=output.ops
            ),
            manifest_hash="a" * 64,
            context_hash="b" * 64,
            brief_updates=tuple(updates),
            brief_route="editor_ops",
            brief_expected_version=1,
            brief_coverage={
                "applicable_ids": ["r1"],
                "retrieved_ids": ["r1"],
                "enforced_ids": [],
                "unresolved_ids": ["r1"],
            },
        )

    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", planned)
    try:
        accepted = await _submit(user_id, thread_id, message, 2)
        result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
        assert result["status"] == "completed"
        with sync_session() as db:
            head = db.execute(
                select(CreatorEditDraft).where(
                    CreatorEditDraft.item_id == item.id, CreatorEditDraft.is_head.is_(True)
                )
            ).scalar_one()
            pinned = BriefBinding.model_validate(head.snapshot_json["brief_binding"])
            assert pinned.state == "pinned" and pinned.brief.version == 2
            assert [r.literal for r in pinned.brief.live() if r.literal] == [original, "Lisbon"]
            assert {r.kind for r in pinned.brief.live()} >= {"text", "style"}
            assert message in pinned.creator_request
            rows = head.snapshot_json["editor_payload"]["text_elements"]
            title = next(row for row in rows if row["text"] == original)
            lisbon = next(row for row in rows if row["text"] == "Lisbon")
            assert lisbon["animation_phases"]["entrance"] == phase
            if detailed:
                assert "timing" in {r.kind for r in pinned.brief.live()}
                assert title["animation_phases"]["entrance"] == "typewriter"
                assert lisbon["start_s"] == pytest.approx(title["start_s"] + 0.4)
                assert lisbon["start_s"] < title["end_s"]
            assert db.get(Job, job_id).assembly_plan == assembly
            assert (
                db.execute(
                    select(func.count())
                    .select_from(CreatorAgentApproval)
                    .where(CreatorAgentApproval.turn_id == uuid.UUID(accepted.turn_id))
                ).scalar_one()
                == 0
            )
            assert (
                db.execute(
                    select(func.count())
                    .select_from(CreativeBriefVersion)
                    .where(CreativeBriefVersion.thread_id == thread_id)
                ).scalar_one()
                == 2
            )
        await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
        with sync_session() as db:
            assert (
                db.execute(
                    select(func.count())
                    .select_from(CreativeBriefVersion)
                    .where(CreativeBriefVersion.thread_id == thread_id)
                ).scalar_one()
                == 2
            )
    finally:
        await async_engine.dispose()


async def _await_montage_approval(
    monkeypatch: pytest.MonkeyPatch, *, suffix: str
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, str, int, int]:
    """A pending montage approval: (user, thread, item, approval, token, rev, draft)."""
    user_id, thread_id, _session_id, item_id = _seed_narration_ready_project()
    strategy_plan = adapt_creator_action(
        ProposeStrategy(
            kind="propose_strategy",
            strategy=CreativeStrategy(
                direction="fast_montage",
                edit_format="montage",
                audio_strategy="licensed_music",
                pacing="fast",
                render_program="native",
                selected_media_ids=[],
                rationale="A quick montage.",
            ),
            summary="A quick montage of the footage.",
        )
    )

    async def _planned(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        return PlannedKriaTurn(plan=strategy_plan, manifest_hash="a" * 64, context_hash="b" * 64)

    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", _planned)
    async with AsyncSessionLocal() as db:
        accepted, _ = await submit_turn(
            db,
            thread_id=thread_id,
            creator_id=user_id,
            body=SubmitTurnBody(
                message="Make a quick montage",
                client_event_id=f"shape-{suffix}-{uuid.uuid4().hex}",
                expected_thread_revision=2,
            ),
        )
    result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
    assert result == {"turn_id": accepted.turn_id, "status": "awaiting_approval"}
    with sync_session() as db:
        turn = db.get(CreatorAgentTurn, uuid.UUID(accepted.turn_id))
        approval = db.execute(
            select(CreatorAgentApproval).where(CreatorAgentApproval.turn_id == turn.id)
        ).scalar_one()
        return (
            user_id,
            thread_id,
            item_id,
            approval.id,
            approval_fingerprint(approval),
            db.get(CreationThread, thread_id).revision,
            approval.draft_revision,
        )


@pytest.mark.asyncio
async def test_approval_output_shape_is_validated_stashed_and_applied_at_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KRI-306 full path: a bad choice 422s without side effects; a good one is
    stashed on the execution (fingerprint unchanged), applied to the item only at
    claim time, and dispatch receives it as `creator_render_shape`."""
    monkeypatch.setattr(settings, "ios_device_only_mode", True)
    monkeypatch.setenv("LANDSCAPE_OUTPUT_ENABLED", "false")
    try:
        (
            user_id,
            thread_id,
            item_id,
            approval_id,
            token,
            thread_revision,
            draft_revision,
        ) = await _await_montage_approval(monkeypatch, suffix="approve")

        def _body(**shape: object) -> ApprovalDecisionBody:
            return ApprovalDecisionBody(
                expected_thread_revision=thread_revision,
                expected_draft_revision=draft_revision,
                expected_approval_fingerprint=token,
                **shape,
            )

        # --- 1) landscape is not offered (flag off): 422, nothing changed. ---
        async with AsyncSessionLocal() as db:
            with pytest.raises(RuntimeFailure) as failure:
                await decide_approval(
                    db,
                    thread_id=thread_id,
                    approval_id=approval_id,
                    creator_id=user_id,
                    decision="approve",
                    body=_body(output_orientation="landscape"),
                )
        assert failure.value.status_code == 422
        assert failure.value.code == "render_shape_unsupported"
        with sync_session() as db:
            approval = db.get(CreatorAgentApproval, approval_id)
            assert approval.status == "pending"
            assert approval_fingerprint(approval) == token
            execution = db.get(CreatorAgentExecution, uuid.UUID(approval.execution_ids[0]))
            assert "render_shape" not in (execution.result or {})
            assert db.get(PlanItem, item_id).landscape_fit == "fit"

        # --- 2) a valid portrait/crop choice: approved, stashed, item untouched. ---
        async with AsyncSessionLocal() as db:
            decision, _ = await decide_approval(
                db,
                thread_id=thread_id,
                approval_id=approval_id,
                creator_id=user_id,
                decision="approve",
                body=_body(output_orientation="portrait", landscape_fit="fill"),
            )
        assert decision.status == "approved"
        with sync_session() as db:
            approval = db.get(CreatorAgentApproval, approval_id)
            assert approval_fingerprint(approval) == token  # never part of the fingerprint
            execution = db.get(CreatorAgentExecution, uuid.UUID(approval.execution_ids[0]))
            assert execution.result["render_shape"] == {
                "output_orientation": "portrait",
                "landscape_fit": "fill",
            }
            assert db.get(PlanItem, item_id).landscape_fit == "fit"  # applied at claim

        # --- 3) claim applies it; dispatch receives it. ---
        captured: dict[str, object] = {}

        def _fake_dispatch(*args, **kwargs):  # noqa: ANN002, ANN003
            captured["kwargs"] = kwargs
            return DispatchResult("publish_failed", job_id=None)

        monkeypatch.setattr("app.tasks.content_plan_build.dispatch_item_render_for", _fake_dispatch)
        await asyncio.to_thread(execute_kria_approval.run, str(approval_id))
        assert captured["kwargs"]["creator_render_shape"] == {
            "output_orientation": "portrait",
            "landscape_fit": "fill",
        }
        with sync_session() as db:
            assert db.get(PlanItem, item_id).landscape_fit == "fill"
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_approval_without_a_shape_choice_dispatches_unchanged_and_deny_ignores_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ios_device_only_mode", True)
    try:
        (
            user_id,
            thread_id,
            item_id,
            approval_id,
            token,
            thread_revision,
            draft_revision,
        ) = await _await_montage_approval(monkeypatch, suffix="plain")
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
        captured: dict[str, object] = {}

        def _fake_dispatch(*args, **kwargs):  # noqa: ANN002, ANN003
            captured["kwargs"] = kwargs
            return DispatchResult("publish_failed", job_id=None)

        monkeypatch.setattr("app.tasks.content_plan_build.dispatch_item_render_for", _fake_dispatch)
        await asyncio.to_thread(execute_kria_approval.run, str(approval_id))
        assert "creator_render_shape" not in captured["kwargs"]  # byte-identical call
        with sync_session() as db:
            assert db.get(PlanItem, item_id).landscape_fit == "fit"
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_deny_with_a_shape_choice_leaves_the_item_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ios_device_only_mode", True)
    try:
        (
            user_id,
            thread_id,
            item_id,
            approval_id,
            token,
            thread_revision,
            draft_revision,
        ) = await _await_montage_approval(monkeypatch, suffix="deny")
        async with AsyncSessionLocal() as db:
            await decide_approval(
                db,
                thread_id=thread_id,
                approval_id=approval_id,
                creator_id=user_id,
                decision="deny",
                body=ApprovalDecisionBody(
                    expected_thread_revision=thread_revision,
                    expected_draft_revision=draft_revision,
                    expected_approval_fingerprint=token,
                    landscape_fit="fill",
                ),
            )
        with sync_session() as db:
            approval = db.get(CreatorAgentApproval, approval_id)
            assert approval.status == "denied"
            execution = db.get(CreatorAgentExecution, uuid.UUID(approval.execution_ids[0]))
            assert "render_shape" not in (execution.result or {})
            assert db.get(PlanItem, item_id).landscape_fit == "fit"
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_landscape_choice_reaches_dispatch_and_keeps_the_bars_preference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ios_device_only_mode", True)
    monkeypatch.setenv("LANDSCAPE_OUTPUT_ENABLED", "true")
    try:
        (
            user_id,
            thread_id,
            item_id,
            approval_id,
            token,
            thread_revision,
            draft_revision,
        ) = await _await_montage_approval(monkeypatch, suffix="landscape")
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
                    output_orientation="landscape",
                    landscape_fit="fit",
                ),
            )
        captured: dict[str, object] = {}

        def _fake_dispatch(*args, **kwargs):  # noqa: ANN002, ANN003
            captured["kwargs"] = kwargs
            return DispatchResult("publish_failed", job_id=None)

        monkeypatch.setattr("app.tasks.content_plan_build.dispatch_item_render_for", _fake_dispatch)
        await asyncio.to_thread(execute_kria_approval.run, str(approval_id))
        # Landscape never has bars: the stored shape says crop, and the item's
        # remembered bars/crop preference is left alone for later portrait edits.
        assert captured["kwargs"]["creator_render_shape"] == {
            "output_orientation": "landscape",
            "landscape_fit": "fill",
        }
        with sync_session() as db:
            assert db.get(PlanItem, item_id).landscape_fit == "fit"
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_thread_projects_render_shape_only_while_a_strategy_approval_is_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routes.creation_threads import _response

    monkeypatch.setattr(settings, "ios_device_only_mode", True)
    monkeypatch.setenv("LANDSCAPE_OUTPUT_ENABLED", "true")
    try:
        (
            user_id,
            thread_id,
            _item,
            approval_id,
            token,
            thread_revision,
            draft_revision,
        ) = await _await_montage_approval(monkeypatch, suffix="projection")
        async with AsyncSessionLocal() as db:
            projected = await _response(db, await db.get(CreationThread, thread_id))
        assert projected.render_shape is not None
        assert projected.render_shape.model_dump() == {
            "orientations": ["portrait", "landscape"],
            "fit_choices": ["fit", "fill"],
            "default": {"output_orientation": "portrait", "landscape_fit": "fit"},
        }

        # Flag off: landscape disappears from the offer, bars/crop stays.
        monkeypatch.setenv("LANDSCAPE_OUTPUT_ENABLED", "false")
        async with AsyncSessionLocal() as db:
            projected = await _response(db, await db.get(CreationThread, thread_id))
        assert projected.render_shape.orientations == ["portrait"]

        # Decided (no longer pending): nothing to offer.
        async with AsyncSessionLocal() as db:
            await decide_approval(
                db,
                thread_id=thread_id,
                approval_id=approval_id,
                creator_id=user_id,
                decision="deny",
                body=ApprovalDecisionBody(
                    expected_thread_revision=thread_revision,
                    expected_draft_revision=draft_revision,
                    expected_approval_fingerprint=token,
                ),
            )
        async with AsyncSessionLocal() as db:
            projected = await _response(db, await db.get(CreationThread, thread_id))
        assert projected.render_shape is None
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_thread_without_a_pending_strategy_projects_no_render_shape() -> None:
    from app.routes.creation_threads import _response

    _user, thread_id, _session, _item = _seed_narration_ready_project()
    try:
        async with AsyncSessionLocal() as db:
            projected = await _response(db, await db.get(CreationThread, thread_id))
        assert projected.render_shape is None
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_a_stale_shape_stash_is_cleared_when_a_retry_sends_no_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A committed-then-refused first attempt must not leak its shape into the claim."""
    monkeypatch.setattr(settings, "ios_device_only_mode", True)
    try:
        (
            user_id,
            thread_id,
            _item,
            approval_id,
            token,
            thread_revision,
            draft_revision,
        ) = await _await_montage_approval(monkeypatch, suffix="stale-stash")
        with sync_session() as db:
            approval = db.get(CreatorAgentApproval, approval_id)
            execution = db.get(CreatorAgentExecution, uuid.UUID(approval.execution_ids[0]))
            execution.result = {
                **(execution.result or {}),
                "render_shape": {"output_orientation": "portrait", "landscape_fit": "fill"},
            }
            db.commit()
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
        with sync_session() as db:
            approval = db.get(CreatorAgentApproval, approval_id)
            execution = db.get(CreatorAgentExecution, uuid.UUID(approval.execution_ids[0]))
            assert "render_shape" not in (execution.result or {})
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_a_shape_choice_on_a_possible_speech_montage_is_not_applicable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The spoken-excerpt montage ignores the shape, so a choice is refused, not dropped.

    Legacy routing (raw-text gate). With plan authority on, the offer follows the typed plan's
    camera-audio sources instead (KRI-470 PR-F; tests/services/test_render_shape.py)."""
    monkeypatch.setattr(settings, "ios_device_only_mode", True)
    monkeypatch.setattr(settings, "kria_plan_authority_enabled", False)
    monkeypatch.setattr(settings, "speech_excerpt_montage_enabled", True)
    monkeypatch.setenv("LANDSCAPE_OUTPUT_ENABLED", "true")
    try:
        (
            user_id,
            thread_id,
            item_id,
            approval_id,
            token,
            thread_revision,
            draft_revision,
        ) = await _await_montage_approval(monkeypatch, suffix="speech")
        with sync_session() as db:
            item = db.get(PlanItem, item_id)
            item.voiceover_gcs_path = None  # a voiceover montage never takes this lane
            item.clip_assignments = [
                {
                    "gcs_path": "users/u/talk.mp4",
                    "kind": "video",
                    "analysis": {"understanding": {"speech": {"has_speech": True}}},
                }
            ]
            db.commit()
        async with AsyncSessionLocal() as db:
            projected = await _response_for(db, thread_id)
        assert projected.render_shape is None
        async with AsyncSessionLocal() as db:
            with pytest.raises(RuntimeFailure) as failure:
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
                        output_orientation="landscape",
                    ),
                )
        assert failure.value.status_code == 422
        assert failure.value.code == "render_shape_not_applicable"
    finally:
        await async_engine.dispose()


async def _response_for(db, thread_id):  # noqa: ANN001, ANN202
    from app.routes.creation_threads import _response

    return await _response(db, await db.get(CreationThread, thread_id))
