"""Real-Postgres integration coverage for the Kria runtime-v2 durability seam.

These tests intentionally cross the async HTTP-service/sync Celery boundary.
Mocks cannot prove that one accepted turn survives the handoff, owns a database
lease, writes a real execution receipt, and is observable through the delta API.
Each test uses fresh UUID rows, so the module is xdist-safe without truncation.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, text
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
    _claim_approval_dispatch,
    _observe_dispatched_execution,
    execute_kria_approval,
    prune_kria_drafts,
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
    [(True, None), (False, "style"), (False, "stale_head"), (False, "speech")],
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
        session.target_generation_id = "generation-1"
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
                    draft.base_generation_id = "older-generation"
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
        _snapshot, _message, lease_epoch, revision = claimed
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
        _snapshot, _message, lease_epoch, revision = claimed
        done = await asyncio.to_thread(
            lambda: _complete_response_turn(
                turn_id,
                lease_owner="owner-2",
                lease_epoch=lease_epoch,
                claimed_thread_revision=revision,
                plan=question,
                brief_updates=updates,
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
