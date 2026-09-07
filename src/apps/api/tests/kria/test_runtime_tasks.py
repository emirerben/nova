from __future__ import annotations

import uuid
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

from app.config import settings
from app.kria.contracts import KriaObservedTurnResponse, KriaToolReceipt, KriaTurnPlan
from app.tasks.kria_runtime import (
    _claim,
    _complete_read_turn,
    _Completion,
    _fail_turn,
    _validate_draft_plan,
    execute_kria_approval,
    prune_kria_drafts,
    reconcile_kria_turns,
    run_kria_turn,
)


class _Scalars:
    def __init__(self, values: list[object]) -> None:
        self._values = values

    def __iter__(self):  # noqa: ANN204
        return iter(self._values)


class _Result:
    def __init__(self, *, scalar=None, scalars=None):  # noqa: ANN001
        self._scalar = scalar
        self._scalars = list(scalars or [])

    def scalar_one_or_none(self):  # noqa: ANN201
        return self._scalar

    def scalar_one(self):  # noqa: ANN201
        return self._scalar

    def scalars(self) -> _Scalars:
        return _Scalars(self._scalars)


@pytest.fixture(autouse=True)
def _runtime_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)


def _draft_plan(*, render_dependencies: list[str]) -> KriaTurnPlan:
    return KriaTurnPlan(
        mode="act",
        turn_value="action",
        intents=[
            {
                "intent_id": "draft",
                "tool_name": "draft.apply_editor_ops",
                "tool_version": 1,
                "arguments": {
                    "operations": [{"op": "set_title", "title": "Matcha restock"}],
                    "summary": "Open on the whisk and end on the first sip.",
                },
            },
            {
                "intent_id": "render",
                "tool_name": "render.request",
                "tool_version": 1,
                "depends_on": render_dependencies,
            },
        ],
    )


def test_draft_plan_accepts_only_the_atomic_draft_then_approval_group() -> None:
    _validate_draft_plan(_draft_plan(render_dependencies=["draft"]))


def test_draft_plan_rejects_render_not_pinned_to_the_exact_draft_group() -> None:
    with pytest.raises(RuntimeError, match="exact draft bundle"):
        _validate_draft_plan(_draft_plan(render_dependencies=[]))


def test_draft_pruning_preserves_every_approval_protected_body() -> None:
    now = datetime.now(UTC)
    protected = [
        SimpleNamespace(id=uuid.uuid4(), snapshot_json={"kind": "strategy"})
        for _status in ("pending", "approved", "consumed")
    ]
    free = SimpleNamespace(id=uuid.uuid4(), snapshot_json={"kind": "strategy"})
    db = MagicMock()
    db.execute.side_effect = [
        _Result(scalar=now),
        _Result(scalars=[*protected, free]),
        _Result(scalars=[row.id for row in protected]),
    ]

    with patch("app.tasks.kria_runtime.sync_session", return_value=nullcontext(db)):
        result = prune_kria_drafts.run()

    assert result == {"pruned": 1}
    assert all(row.snapshot_json is not None for row in protected)
    assert free.snapshot_json is None
    protected_query = str(
        db.execute.call_args_list[2].args[0].compile(compile_kwargs={"literal_binds": True})
    )
    assert "pending" in protected_query
    assert "approved" in protected_query
    assert "consumed" in protected_query
    db.commit.assert_called_once_with()


def test_reconciler_selects_only_recoverable_turns_and_publishes_stable_task_ids() -> None:
    now = datetime.now(UTC)
    turns = [
        SimpleNamespace(
            id=uuid.uuid4(), status="pending", lease_owner="old", lease_expires_at=None
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            status="planning",
            lease_owner="stale-worker",
            lease_expires_at=now - timedelta(seconds=1),
        ),
    ]
    db = MagicMock()
    db.execute.side_effect = [
        _Result(scalar=now),
        _Result(scalars=turns),
        _Result(scalars=[]),
        _Result(scalars=[]),
        _Result(scalars=[]),
    ]

    with (
        patch("app.tasks.kria_runtime.sync_session", return_value=nullcontext(db)),
        patch("app.tasks.kria_runtime.run_kria_turn.apply_async") as publish,
    ):
        result = reconcile_kria_turns.run()

    statement = db.execute.call_args_list[1].args[0]
    sql = str(statement.compile(compile_kwargs={"literal_binds": True}))
    assert "creator_agent_turns.status = 'pending'" in sql
    assert "creator_agent_turns.status = 'planning'" in sql
    assert "creator_agent_turns.lease_expires_at < now()" in sql
    assert "creator_agent_turns.status = 'queued'" not in sql
    assert statement._limit_clause.value == 50  # noqa: SLF001 - query contract
    assert statement._for_update_arg.skip_locked is True  # noqa: SLF001 - claim contract
    assert result == {"published": 2, "settled": 0}
    publish.assert_has_calls(
        [call(args=[str(turn.id)], task_id=str(turn.id), queue="agent-control") for turn in turns]
    )
    assert all(turn.status == "pending" for turn in turns)
    assert all(turn.lease_owner is None for turn in turns)
    assert all(turn.lease_expires_at == now + timedelta(minutes=1) for turn in turns)
    db.commit.assert_called_once_with()


def test_reconciler_republishes_accepted_approval_execution() -> None:
    approval_id = str(uuid.uuid4())
    execution = SimpleNamespace(
        id=uuid.uuid4(),
        accepted_at=datetime.now(UTC),
        result={"approval_id": approval_id},
    )
    db = MagicMock()
    db.execute.side_effect = [
        _Result(scalar=datetime.now(UTC)),
        _Result(scalars=[]),
        _Result(scalars=[execution]),
        _Result(scalars=[]),
        _Result(scalars=[]),
    ]

    with (
        patch("app.tasks.kria_runtime.sync_session", return_value=nullcontext(db)),
        patch("app.tasks.kria_runtime.execute_kria_approval.apply_async") as publish,
    ):
        result = reconcile_kria_turns.run()

    assert result == {"published": 1, "settled": 0}
    publish.assert_called_once_with(
        args=[approval_id],
        task_id=f"kria-approval:{approval_id}",
        queue="agent-control",
    )


def test_reconciler_recovers_approved_execution_after_initial_publish_failure() -> None:
    approval_id = uuid.uuid4()
    db = MagicMock()
    db.execute.side_effect = [
        _Result(scalar=datetime.now(UTC)),
        _Result(scalars=[]),
        _Result(scalars=[]),
        _Result(scalars=[approval_id]),
        _Result(scalars=[]),
    ]

    with (
        patch("app.tasks.kria_runtime.sync_session", return_value=nullcontext(db)),
        patch("app.tasks.kria_runtime.execute_kria_approval.apply_async") as publish,
    ):
        result = reconcile_kria_turns.run()

    approved_query = db.execute.call_args_list[3].args[0]
    approved_sql = str(approved_query.compile(compile_kwargs={"literal_binds": True}))
    assert "creator_agent_approvals.status = 'approved'" in approved_sql
    assert approved_query._for_update_arg.skip_locked is True  # noqa: SLF001
    assert result == {"published": 1, "settled": 0}
    publish.assert_called_once_with(
        args=[str(approval_id)],
        task_id=f"kria-approval:{approval_id}",
        queue="agent-control",
    )


def test_failed_turn_projects_a_durable_receipt_linked_recovery_event() -> None:
    now = datetime.now(UTC)
    turn = SimpleNamespace(
        id=uuid.uuid4(),
        thread_id=uuid.uuid4(),
        status="planning",
        lease_owner="worker-1",
        lease_epoch=3,
        lease_expires_at=now + timedelta(seconds=10),
        error=None,
        completed_at=None,
        observed_event_id=None,
    )
    thread = SimpleNamespace(id=turn.thread_id)
    event = SimpleNamespace(id=uuid.uuid4())
    db = MagicMock()
    db.execute.side_effect = [
        _Result(scalar=turn),
        _Result(scalar=now),
        _Result(scalar=thread),
    ]

    with (
        patch("app.tasks.kria_runtime.sync_session", return_value=nullcontext(db)),
        patch("app.tasks.kria_runtime._append_sync_event", return_value=event) as append,
        patch("app.tasks.kria_runtime._promote_queued_successor_sync", return_value=None),
    ):
        assert (
            _fail_turn(
                turn.id,
                code="runtime_turn_failed",
                lease_owner="worker-1",
                lease_epoch=3,
            )
            is None
        )

    assert turn.status == "failed"
    assert turn.observed_event_id == event.id
    append.assert_called_once()
    assert append.call_args.kwargs["event_type"] == "assistant_error"
    assert append.call_args.kwargs["payload"]["receipt_ids"] == []
    assert append.call_args.kwargs["payload"]["recovery"] == "retry"
    db.commit.assert_called_once_with()


def test_execute_approval_dispatches_only_the_claimed_server_strategy() -> None:
    approval_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())
    claim = SimpleNamespace(
        item_id=uuid.uuid4(),
        ownership_epoch=4,
        strategy={"edit_format": "day_vlog", "pacing": "fast"},
        creator_request="Open on the whisk.",
    )
    dispatch_result = SimpleNamespace(outcome="dispatched", job_id=job_id)

    with (
        patch("app.tasks.kria_runtime._claim_approval_dispatch", return_value=claim),
        patch(
            "app.tasks.content_plan_build.dispatch_item_render_for",
            return_value=dispatch_result,
        ) as dispatch,
        patch(
            "app.tasks.kria_runtime._finish_approval_dispatch",
            return_value=("dispatched", None),
        ) as finish,
    ):
        result = execute_kria_approval.run(approval_id)

    assert result == {"approval_id": approval_id, "status": "dispatched", "job_id": job_id}
    dispatch.assert_called_once_with(
        str(claim.item_id),
        4,
        bypass_guided_edit_gate=True,
        creator_strategy=claim.strategy,
        creator_request=claim.creator_request,
    )
    finish.assert_called_once_with(claim, outcome="dispatched", job_id=job_id)


def test_execute_approval_enqueues_exact_committed_editor_generation() -> None:
    approval_id = str(uuid.uuid4())
    job_id = uuid.uuid4()
    prep = {"generation": "gen-editor-2", "has_render_section": True}
    claim = SimpleNamespace(
        draft_kind="editor",
        target_job_id=job_id,
        target_variant_id="original_text",
        target_generation_id="gen-editor-2",
        editor_prep=prep,
    )

    with (
        patch("app.tasks.kria_runtime._claim_approval_dispatch", return_value=claim),
        patch("app.tasks.kria_runtime.enqueue_editor_commit_render") as enqueue,
        patch(
            "app.tasks.kria_runtime._finish_approval_dispatch",
            return_value=("dispatched", None),
        ) as finish,
    ):
        result = execute_kria_approval.run(approval_id)

    assert result == {
        "approval_id": approval_id,
        "status": "dispatched",
        "job_id": str(job_id),
    }
    enqueue.assert_called_once_with(str(job_id), "original_text", prep)
    finish.assert_called_once_with(claim, outcome="dispatched", job_id=str(job_id))


def test_execute_editor_publish_exception_is_outcome_unknown_not_retried() -> None:
    approval_id = str(uuid.uuid4())
    job_id = uuid.uuid4()
    claim = SimpleNamespace(
        draft_kind="editor",
        target_job_id=job_id,
        target_variant_id="subtitled",
        target_generation_id="gen-caption-2",
        editor_prep={"generation": "gen-caption-2", "has_render_section": True},
    )

    with (
        patch("app.tasks.kria_runtime._claim_approval_dispatch", return_value=claim),
        patch(
            "app.tasks.kria_runtime.enqueue_editor_commit_render",
            side_effect=RuntimeError("connection closed after publish"),
        ),
        patch(
            "app.tasks.kria_runtime._finish_approval_dispatch",
            return_value=("outcome_unknown", None),
        ) as finish,
    ):
        result = execute_kria_approval.run(approval_id)

    assert result["status"] == "outcome_unknown"
    finish.assert_called_once_with(claim, outcome="outcome_unknown", job_id=str(job_id))


def test_kill_switch_stops_new_claims_and_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", False)
    with (
        patch("app.tasks.kria_runtime.sync_session") as sessions,
        patch("app.tasks.kria_runtime.run_kria_turn.apply_async") as publish,
    ):
        assert _claim(uuid.uuid4(), "disabled-worker") is None
        assert reconcile_kria_turns.run() == {"published": 0, "settled": 0}

    sessions.assert_not_called()
    publish.assert_not_called()


def test_claim_owns_pending_turn_with_database_time_and_returns_trusted_snapshot() -> None:
    now = datetime.now(UTC)
    turn = SimpleNamespace(
        status="pending",
        lease_expires_at=None,
        cancel_requested_at=None,
        lease_owner=None,
        lease_epoch=0,
        completed_at=None,
        thread_id=uuid.uuid4(),
        source_event_id=uuid.uuid4(),
    )
    thread = SimpleNamespace(
        revision=7,
        state={
            "media": [{"filename": "whisking.mov", "gcs_path": "private/input.mov"}],
            "edit_format": "montage",
            "strongest_moment": "whisking",
        },
    )
    source = SimpleNamespace(content="Open with the whisk")
    db = MagicMock()
    db.execute.side_effect = [_Result(scalar=turn), _Result(scalar=now)]
    db.get.side_effect = [thread, source]

    with patch("app.tasks.kria_runtime.sync_session", return_value=nullcontext(db)):
        claimed = _claim(uuid.uuid4(), "task-7")

    assert claimed == (
        {
            "thread_id": "",
            "creator_id": "",
            "item_id": None,
            "media_labels": ["whisking.mov"],
            "edit_format": "montage",
            "strongest_moment": "whisking",
            "editorial_decision": None,
        },
        "Open with the whisk",
        1,
        7,
    )
    assert turn.status == "planning"
    assert turn.lease_owner == "task-7"
    assert turn.lease_epoch == 1
    assert turn.lease_expires_at > now
    db.commit.assert_called_once_with()


def test_claim_excludes_a_live_planning_lease() -> None:
    now = datetime.now(UTC)
    turn = SimpleNamespace(
        status="planning",
        lease_expires_at=now + timedelta(seconds=5),
        cancel_requested_at=None,
    )
    db = MagicMock()
    db.execute.side_effect = [_Result(scalar=turn), _Result(scalar=now)]

    with patch("app.tasks.kria_runtime.sync_session", return_value=nullcontext(db)):
        assert _claim(uuid.uuid4(), "losing-task") is None

    db.commit.assert_not_called()
    db.get.assert_not_called()


def test_claim_projects_preexisting_cancellation_without_running_work() -> None:
    now = datetime.now(UTC)
    turn = SimpleNamespace(
        status="pending",
        lease_expires_at=None,
        cancel_requested_at=now,
        completed_at=None,
    )
    db = MagicMock()
    db.execute.side_effect = [_Result(scalar=turn), _Result(scalar=now)]

    with patch("app.tasks.kria_runtime.sync_session", return_value=nullcontext(db)):
        assert _claim(uuid.uuid4(), "cancelled-task") is None

    assert turn.status == "cancelled"
    assert turn.completed_at is not None
    db.commit.assert_called_once_with()
    db.get.assert_not_called()


def test_complete_read_turn_commits_observation_then_promotes_successor() -> None:
    now = datetime.now(UTC)
    turn = SimpleNamespace(
        id=uuid.uuid4(),
        thread_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        request_digest="digest",
        status="planning",
        lease_owner="task-9",
        lease_epoch=4,
        lease_expires_at=now + timedelta(seconds=5),
        cancel_requested_at=None,
        plan_json=None,
        observed_event_id=None,
        completed_at=None,
    )
    session = SimpleNamespace(id=turn.session_id, revision=3, manifest_hash="manifest-3")
    thread = SimpleNamespace(id=turn.thread_id, revision=7)
    db = MagicMock()
    db.execute.side_effect = [
        _Result(scalar=turn),
        _Result(scalar=now),
        _Result(scalar=None),
        _Result(scalar=thread),
        _Result(scalar=12),
    ]
    db.get.return_value = session
    added: list[object] = []
    db.add.side_effect = added.append

    def _flush() -> None:
        for value in added:
            if getattr(value, "id", None) is None:
                value.id = uuid.uuid4()

    db.flush.side_effect = _flush
    plan = KriaTurnPlan(
        mode="act",
        turn_value="decision",
        intents=[
            {
                "intent_id": "inspect-project",
                "tool_name": "project.inspect",
                "tool_version": 1,
            }
        ],
    )
    receipt = KriaToolReceipt(
        intent_id="inspect-project",
        tool_name="project.inspect",
        tool_version=1,
        status="completed",
        result={"editorial_decision": "Open with the whisk."},
    )
    response = KriaObservedTurnResponse(
        turn_value="decision",
        message="Open with the whisk.",
        next_actions=["prepare_draft"],
    )
    successor_id = str(uuid.uuid4())

    with (
        patch("app.tasks.kria_runtime.sync_session", return_value=nullcontext(db)),
        patch(
            "app.tasks.kria_runtime._promote_queued_successor_sync",
            return_value=successor_id,
        ) as promote,
    ):
        completion = _complete_read_turn(
            turn.id,
            lease_owner="task-9",
            lease_epoch=4,
            claimed_thread_revision=7,
            plan=plan,
            receipt=receipt,
            response=response,
        )

    assert completion.committed is True
    assert completion.successor_turn_id == successor_id
    assert turn.status == "completed"
    assert turn.lease_owner is None
    assert turn.lease_expires_at is None
    assert turn.plan_json == plan.model_dump(mode="json")
    assert added[0].session_id == session.id
    assert added[0].status == "completed"
    assert turn.observed_event_id == added[1].id
    assert thread.revision == 8
    assert added[1].sequence == 13
    assert added[1].payload["receipt_ids"] == [str(added[0].id)]
    db.commit.assert_called_once_with()
    promote.assert_called_once_with(turn.thread_id)


def test_complete_read_turn_refuses_unbacked_receipt() -> None:
    now = datetime.now(UTC)
    turn = SimpleNamespace(
        id=uuid.uuid4(),
        thread_id=uuid.uuid4(),
        session_id=None,
        status="planning",
        lease_owner="task-10",
        lease_epoch=1,
        lease_expires_at=now + timedelta(seconds=5),
        cancel_requested_at=None,
    )
    db = MagicMock()
    db.execute.side_effect = [_Result(scalar=turn), _Result(scalar=now)]
    plan = KriaTurnPlan(
        mode="act",
        turn_value="decision",
        intents=[
            {
                "intent_id": "inspect-project",
                "tool_name": "project.inspect",
                "tool_version": 1,
            }
        ],
    )
    receipt = KriaToolReceipt(
        intent_id="inspect-project",
        tool_name="project.inspect",
        tool_version=1,
        status="completed",
        result={"editorial_decision": "Open with the whisk."},
    )
    response = KriaObservedTurnResponse(
        turn_value="decision",
        message="Open with the whisk.",
    )

    with (
        patch("app.tasks.kria_runtime.sync_session", return_value=nullcontext(db)),
        patch("app.tasks.kria_runtime._promote_queued_successor_sync") as promote,
    ):
        try:
            _complete_read_turn(
                turn.id,
                lease_owner="task-10",
                lease_epoch=1,
                claimed_thread_revision=0,
                plan=plan,
                receipt=receipt,
                response=response,
            )
        except RuntimeError as exc:
            assert "durable receipt session" in str(exc)
        else:  # pragma: no cover - assertion guard
            raise AssertionError("session-less completion must fail")

    db.commit.assert_not_called()
    db.add.assert_not_called()
    promote.assert_not_called()


def test_complete_read_turn_rejects_a_stale_lease_without_projection() -> None:
    now = datetime.now(UTC)
    turn = SimpleNamespace(
        status="planning",
        lease_owner="new-owner",
        lease_epoch=5,
        lease_expires_at=now + timedelta(seconds=5),
    )
    db = MagicMock()
    db.execute.side_effect = [_Result(scalar=turn), _Result(scalar=now)]
    plan = KriaTurnPlan(
        mode="act",
        turn_value="decision",
        intents=[
            {
                "intent_id": "inspect-project",
                "tool_name": "project.inspect",
                "tool_version": 1,
            }
        ],
    )
    receipt = KriaToolReceipt(
        intent_id="inspect-project",
        tool_name="project.inspect",
        tool_version=1,
        status="completed",
        result={"editorial_decision": "Open with the whisk."},
    )
    response = KriaObservedTurnResponse(
        turn_value="decision",
        message="Open with the whisk.",
    )

    with (
        patch("app.tasks.kria_runtime.sync_session", return_value=nullcontext(db)),
        patch("app.tasks.kria_runtime._promote_queued_successor_sync") as promote,
    ):
        completion = _complete_read_turn(
            uuid.uuid4(),
            lease_owner="stale-owner",
            lease_epoch=4,
            claimed_thread_revision=0,
            plan=plan,
            receipt=receipt,
            response=response,
        )

    assert completion.committed is False
    assert completion.successor_turn_id is None
    db.commit.assert_not_called()
    db.add.assert_not_called()
    promote.assert_not_called()


def test_complete_read_turn_requeues_when_project_revision_changed() -> None:
    now = datetime.now(UTC)
    turn = SimpleNamespace(
        id=uuid.uuid4(),
        thread_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        request_digest="digest",
        status="planning",
        lease_owner="task-stale-snapshot",
        lease_epoch=2,
        lease_expires_at=now + timedelta(seconds=5),
        cancel_requested_at=None,
    )
    session = SimpleNamespace(id=turn.session_id, revision=1, manifest_hash="manifest")
    thread = SimpleNamespace(id=turn.thread_id, revision=9)
    db = MagicMock()
    db.execute.side_effect = [
        _Result(scalar=turn),
        _Result(scalar=now),
        _Result(scalar=None),
        _Result(scalar=thread),
    ]
    db.get.return_value = session
    added: list[object] = []
    db.add.side_effect = added.append

    def _flush() -> None:
        added[0].id = uuid.uuid4()

    db.flush.side_effect = _flush
    plan = KriaTurnPlan(
        mode="act",
        turn_value="decision",
        intents=[
            {
                "intent_id": "inspect-project",
                "tool_name": "project.inspect",
                "tool_version": 1,
            }
        ],
    )
    receipt = KriaToolReceipt(
        intent_id="inspect-project",
        tool_name="project.inspect",
        tool_version=1,
        status="completed",
        result={"editorial_decision": "Outdated decision"},
    )
    response = KriaObservedTurnResponse(turn_value="decision", message="Outdated decision")

    with patch("app.tasks.kria_runtime.sync_session", return_value=nullcontext(db)):
        completion = _complete_read_turn(
            turn.id,
            lease_owner="task-stale-snapshot",
            lease_epoch=2,
            claimed_thread_revision=8,
            plan=plan,
            receipt=receipt,
            response=response,
        )

    assert completion == _Completion(committed=False, requeue_turn_id=str(turn.id))
    assert turn.status == "pending"
    assert turn.lease_owner is None
    assert turn.lease_expires_at is None
    db.delete.assert_called_once_with(added[0])
    db.commit.assert_called_once_with()


def test_worker_republishes_a_turn_requeued_after_snapshot_drift() -> None:
    turn_id = str(uuid.uuid4())
    with (
        patch(
            "app.tasks.kria_runtime._claim",
            return_value=({"media_labels": ["clip.mov"]}, "Make it warm", 3, 8),
        ),
        patch(
            "app.tasks.kria_runtime._complete_read_turn",
            return_value=_Completion(committed=False, requeue_turn_id=turn_id),
        ),
        patch("app.tasks.kria_runtime.run_kria_turn.apply_async") as publish,
    ):
        result = run_kria_turn.run(turn_id)

    assert result == {"turn_id": turn_id, "status": "requeued"}
    publish.assert_called_once_with(args=[turn_id], task_id=turn_id, queue="agent-control")
