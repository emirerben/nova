"""Offline durable-receipt recovery; an unknown enqueue is never sent again."""

import uuid
from contextlib import nullcontext
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace as Row
from unittest.mock import MagicMock

import pytest
from sqlalchemy import Column, DateTime, MetaData, String, Table, Uuid, create_engine

from app.models import CreatorAgentExecution, CreatorAgentSession, PlanItem
from app.tasks import kria_runtime as rt


@pytest.fixture
def receipt(monkeypatch):
    owner = uuid.uuid4()
    error = {"code": "render_dispatch_outcome_unknown", "retryable": False, "recovery": "manual"}
    plan = Row(id=uuid.uuid4(), user_id=owner, ownership_epoch=1)
    job = Row(
        id=uuid.uuid4(),
        user_id=owner,
        status="variants_ready",
        content_plan_ownership_epoch=1,
        assembly_plan={
            "variants": [
                {
                    "variant_id": "cut",
                    "render_generation_id": "new",
                    "render_status": "ready",
                    "video_path": "owned/result.mp4",
                }
            ]
        },
    )
    item = Row(id=uuid.uuid4(), content_plan_id=plan.id, current_job_id=job.id)
    job.content_plan_item_id = item.id
    session = Row(
        id=uuid.uuid4(),
        creator_id=owner,
        plan_item_id=item.id,
        target_job_id=job.id,
        ownership_epoch=1,
        status="awaiting_feedback",
        last_error=error,
        last_good=None,
    )
    turn = Row(id=uuid.uuid4(), status="failed", error=error, created_at=datetime.now(UTC))
    thread = Row(
        id=uuid.uuid4(),
        creator_id=owner,
        runtime_version=2,
        active_creator_agent_session_id=session.id,
        active_plan_item_id=item.id,
        state={},
    )
    execution = Row(
        id=uuid.uuid4(),
        status="outcome_unknown",
        target_job_id=job.id,
        turn_id=turn.id,
        session_id=session.id,
        target_thread_id=thread.id,
        target_variant_id="cut",
        target_generation_id="new",
        target_draft_id=None,
        error=error,
        result={"editor_prep": {"render_destination": "cloud"}},
        observed_at=None,
    )
    db = MagicMock()
    db.get.side_effect = lambda model, _: {
        CreatorAgentExecution: execution,
        CreatorAgentSession: session,
        PlanItem: item,
    }[model]
    locked = [plan, item, job, session, turn, execution, thread]
    state = Row(later_turn=None, locked=locked, graph=list(locked))
    db.execute.side_effect = lambda _: Row(
        scalar_one_or_none=lambda: locked.pop(0) if locked else state.later_turn
    )
    monkeypatch.setattr(rt, "sync_session", lambda: nullcontext(db))
    monkeypatch.setattr(rt, "_bind_thread_language", lambda *_: None)
    events = []

    def append(*_, **event):
        events.append(event)
        return Row(id=uuid.uuid4())

    monkeypatch.setattr(rt, "_append_sync_event", append)

    def forbidden(*_, **__):
        pytest.fail("receipt recovery must not publish work or run generation review")

    for name in ("enqueue_editor_commit_render", "_promote_queued_successor_sync"):
        monkeypatch.setattr(rt, name, forbidden)
    monkeypatch.setattr(rt.run_kria_turn, "apply_async", forbidden)
    monkeypatch.setattr(rt.execute_kria_approval, "apply_async", forbidden)
    return Row(
        execution=execution,
        job=job,
        session=session,
        turn=turn,
        thread=thread,
        item=item,
        db=db,
        events=events,
        state=state,
    )


def test_completed_exact_unknown_receipt_recovers_without_dispatch(receipt):
    assert rt._observe_dispatched_execution(receipt.execution.id) == ("completed", None)
    assert receipt.execution.status == receipt.turn.status == "completed"
    assert receipt.execution.error is None
    assert receipt.turn.error is None
    assert receipt.session.last_error is None
    assert receipt.session.last_good["render_generation_id"] == "new"
    assert [event["event_type"] for event in receipt.events] == [
        "generation_ready",
        "assistant_review",
    ]
    receipt.db.commit.assert_called_once()
    assert rt._observe_dispatched_execution(receipt.execution.id) == ("ignored", None)
    assert len(receipt.events) == 2


@pytest.mark.parametrize(
    "case",
    [
        "older_generation",
        "other_variant",
        "pending",
        "failed",
        "no_generation",
        "new_job",
        "later_turn",
        "device",
        "no_output",
        "job_failed",
        "new_session",
        "other_owner",
        "new_session_state",
        "replaced_source_without_new_turn",
    ],
)
def test_unknown_without_exact_current_completed_receipt_stays_unknown(receipt, case):
    variant = receipt.job.assembly_plan["variants"][0]
    if case == "older_generation":
        variant["render_generation_id"] = "old"
    elif case == "other_variant":
        variant["variant_id"] = "another"
    elif case in {"pending", "failed"}:
        variant["render_status"] = case
    elif case == "no_generation":
        receipt.execution.target_generation_id = None
    elif case == "new_job":
        receipt.item.current_job_id = uuid.uuid4()
    elif case == "later_turn":
        receipt.state.later_turn = uuid.uuid4()
    elif case == "device":
        receipt.execution.result["editor_prep"]["render_destination"] = "device"
    elif case == "no_output":
        variant.pop("video_path")
    elif case == "job_failed":
        receipt.job.status = "processing_failed"
    elif case == "new_session":
        receipt.thread.active_creator_agent_session_id = uuid.uuid4()
    elif case == "other_owner":
        receipt.job.user_id = uuid.uuid4()
    elif case == "new_session_state":
        receipt.session.status = "planning"
    elif case == "replaced_source_without_new_turn":
        from app.services.source_replacement import SOURCE_REPLACEMENT_STATE_KEY, SourceReplacement

        receipt.thread.state = {
            SOURCE_REPLACEMENT_STATE_KEY: SourceReplacement(
                replaced_media_id="old-take",
                media_id="new-take",
                superseded_job_id=str(receipt.job.id),
                revision=9,
            ).to_state()
        }
        # The real replacement route keeps the old Job pointer and creates no new turn.
        assert receipt.item.current_job_id == receipt.job.id
        assert receipt.state.later_turn is None
    before = deepcopy(
        [vars(row) for row in (receipt.execution, receipt.turn, receipt.session, receipt.thread)]
    )
    assert receipt.execution.observed_at is None
    rt._observe_dispatched_execution(receipt.execution.id)
    assert receipt.execution.status == "outcome_unknown"
    assert receipt.turn.status == "failed"
    assert receipt.events == []
    receipt.db.commit.assert_called_once()
    assert receipt.execution.observed_at is not None
    after = deepcopy(
        [vars(row) for row in (receipt.execution, receipt.turn, receipt.session, receipt.thread)]
    )
    after[0]["observed_at"] = before[0]["observed_at"]
    assert after == before


def test_unknown_sweep_reaches_ready_receipt_beyond_fifty_pending_receipts(receipt):
    # Execute the query branches against a disposable in-memory table. Unlike a SQL-text
    # assertion, this exercises NULL ordering, the per-status budget and the second sweep.
    table = Table(
        "creator_agent_executions",
        MetaData(),
        Column("id", Uuid, primary_key=True),
        Column("status", String),
        Column("target_job_id", Uuid),
        Column("turn_id", Uuid),
        Column("target_thread_id", Uuid),
        Column("dispatched_at", DateTime(timezone=True)),
        Column("observed_at", DateTime(timezone=True)),
    )
    engine = create_engine("sqlite://")
    table.metadata.create_all(engine)
    identifiers = [uuid.UUID(int=index) for index in range(52, 103)]
    with engine.begin() as db:
        db.execute(
            table.insert(),
            [
                {
                    "id": identifier,
                    "status": "outcome_unknown",
                    "target_job_id": receipt.job.id,
                    "turn_id": receipt.turn.id,
                    "target_thread_id": receipt.thread.id,
                }
                for identifier in identifiers
            ],
        )
        db.execute(
            table.insert(),
            [
                {
                    "id": uuid.UUID(int=index),
                    "status": "outcome_unknown",
                    "target_job_id": receipt.job.id,
                    "turn_id": None if index % 2 else receipt.turn.id,
                    "target_thread_id": receipt.thread.id if index % 2 else None,
                }
                for index in range(1, 52)
            ],
        )
        query = rt._execution_observation_query()
        unknown_query = query.selects[1].element
        first = list(db.execute(unknown_query).scalars())
        assert first == identifiers[:50]
        variant = receipt.job.assembly_plan["variants"][0]
        variant["render_status"] = "pending"
        for identifier in first:
            receipt.execution.id = identifier
            receipt.execution.observed_at = None
            receipt.state.locked[:] = receipt.state.graph
            assert rt._observe_dispatched_execution(identifier) == ("pending", None)
            db.execute(
                table.update()
                .where(table.c.id == identifier)
                .values(observed_at=receipt.execution.observed_at)
            )
        second = list(db.execute(unknown_query).scalars())
        assert second[0] == identifiers[-1]
        receipt.execution.id = second[0]
        receipt.state.locked[:] = receipt.state.graph
        variant["render_status"] = "ready"
        assert rt._observe_dispatched_execution(second[0]) == ("completed", None)
        assert len(receipt.events) == 2
    engine.dispose()


@pytest.mark.parametrize("sweep", [True, False])
def test_unknown_is_selected_only_for_observation(monkeypatch, sweep):
    monkeypatch.setattr(rt.settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(rt, "_release_stranded_successors", lambda: 0)
    db = MagicMock()
    statements = []

    def execute(statement):
        statements.append(str(statement.compile(compile_kwargs={"literal_binds": True})))
        return Row(scalar_one=lambda: datetime.now(UTC), scalars=lambda: [])

    db.execute.side_effect = execute
    monkeypatch.setattr(rt, "sync_session", lambda: nullcontext(db))
    if sweep:
        assert rt._reconcile_kria_turns_body() == {"published": 0, "settled": 0}
        assert "outcome_unknown" not in statements[2]  # accepted dispatch/republication query
    else:
        assert rt.observe_kria_job.run(str(uuid.uuid4())) == {"settled": 0}
    assert "outcome_unknown" in statements[-1]
    assert statements[-1].count("LIMIT 50") == 2  # unknowns cannot consume dispatched slots


def test_old_source_replacement_marker_does_not_block_new_target_recovery(receipt):
    from app.services.source_replacement import SOURCE_REPLACEMENT_STATE_KEY, SourceReplacement

    receipt.thread.state = {
        SOURCE_REPLACEMENT_STATE_KEY: SourceReplacement(
            replaced_media_id="old-take",
            media_id="new-take",
            superseded_job_id=str(uuid.uuid4()),
            revision=9,
        ).to_state()
    }
    assert rt._observe_dispatched_execution(receipt.execution.id) == ("completed", None)
    assert receipt.execution.status == "completed"
    assert [event["event_type"] for event in receipt.events] == [
        "generation_ready",
        "assistant_review",
    ]
