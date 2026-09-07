from __future__ import annotations

import asyncio
import inspect
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.kria.api_schemas import ApprovalDecisionBody, SubmitTurnBody
from app.kria.contracts import KriaTurnPlan
from app.kria.language import is_help_question, is_status_question
from app.kria.planner import PlannedKriaTurn
from app.kria.runtime import (
    RuntimeFailure,
    approval_fingerprint,
    cancel_turn,
    decide_approval,
    read_delta,
    request_digest,
    submit_turn,
)
from app.tasks.kria_runtime import (
    _complete_read_turn,
    _owns_turn_lease,
    _plan_with_live_agent,
    _renew_turn_lease,
    _snapshot,
    _useful_plan,
    run_kria_turn,
)


class _Result:
    def __init__(self, *, scalar=None, scalars=None):  # noqa: ANN001
        self._scalar = scalar
        self._scalars = list(scalars or [])

    def scalar_one_or_none(self):  # noqa: ANN201
        return self._scalar

    def scalar_one(self):  # noqa: ANN201
        return self._scalar

    def scalars(self):  # noqa: ANN201
        values = self._scalars

        class _Scalars:
            @staticmethod
            def all():  # noqa: ANN205
                return values

            @staticmethod
            def first():  # noqa: ANN205
                return values[0] if values else None

        return _Scalars()


def _thread(*, revision: int = 3) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=uuid.uuid4(),
        runtime_version=2,
        status="active",
        revision=revision,
        active_creator_agent_session_id=None,
        state={},
    )


def _approval(*, thread, draft, creator_id) -> SimpleNamespace:  # noqa: ANN001
    return SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=creator_id,
        thread_id=thread.id,
        session_id=uuid.uuid4(),
        turn_id=uuid.uuid4(),
        draft_id=draft.id,
        draft_revision=draft.draft_revision,
        target_job_id=uuid.uuid4(),
        target_variant_id="song_text",
        target_generation_id="generation-7",
        target_manifest_hash="manifest-hash",
        target_ownership_epoch=4,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        status="pending",
    )


def _session_for(approval) -> SimpleNamespace:  # noqa: ANN001
    return SimpleNamespace(
        id=approval.session_id,
        creator_id=approval.creator_id,
        ownership_epoch=approval.target_ownership_epoch,
        manifest_hash=approval.target_manifest_hash,
        target_job_id=approval.target_job_id,
        target_variant_id=approval.target_variant_id,
        target_generation_id=approval.target_generation_id,
    )


def _turn_for(approval) -> SimpleNamespace:  # noqa: ANN001
    return SimpleNamespace(
        id=approval.turn_id,
        thread_id=approval.thread_id,
        status="awaiting_approval",
        completed_at=None,
    )


def _delta_event(sequence: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        sequence=sequence,
        revision=sequence + 1,
        role="assistant",
        event_type="assistant_response",
        content=f"Update {sequence}",
        payload={"sequence": sequence},
        created_at=datetime.now(UTC),
    )


def _approval_body(approval, *, thread_revision: int, draft_revision: int):  # noqa: ANN001, ANN201
    return ApprovalDecisionBody(
        expected_thread_revision=thread_revision,
        expected_draft_revision=draft_revision,
        expected_approval_fingerprint=approval_fingerprint(approval),
    )


def test_request_digest_binds_normalized_body_and_revision() -> None:
    first = SubmitTurnBody(
        message="Make   the opening faster",
        client_event_id="device-1:turn-4",
        expected_thread_revision=8,
    )
    same = SubmitTurnBody(
        message="Make the opening faster",
        client_event_id="device-1:turn-4",
        expected_thread_revision=8,
    )
    changed_revision = same.model_copy(update={"expected_thread_revision": 9})

    assert request_digest(first) == request_digest(same)
    assert request_digest(first) != request_digest(changed_revision)


def test_approval_fingerprint_changes_when_any_exact_pin_changes() -> None:
    thread = _thread()
    draft = SimpleNamespace(id=uuid.uuid4(), draft_revision=2)
    approval = _approval(thread=thread, draft=draft, creator_id=thread.creator_id)
    original = approval_fingerprint(approval)

    approval.target_generation_id = "generation-8"

    assert approval_fingerprint(approval) != original


@pytest.mark.asyncio
async def test_read_delta_paginates_archived_thread_without_hiding_events() -> None:
    thread = _thread(revision=14)
    thread.status = "archived"
    rows = [_delta_event(5), _delta_event(6), _delta_event(7)]
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _Result(scalar=thread),
                _Result(scalars=rows),
            ]
        )
    )

    response = await read_delta(
        db,
        thread_id=thread.id,
        creator_id=thread.creator_id,
        after_sequence=4,
        limit=2,
    )

    assert response.status == "archived"
    assert response.thread_revision == 14
    assert [event.sequence for event in response.events] == [5, 6]
    assert response.after_sequence == 4
    assert response.next_after_sequence == 6
    assert response.has_more is True


@pytest.mark.asyncio
async def test_read_delta_empty_page_preserves_requested_cursor() -> None:
    thread = _thread(revision=9)
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _Result(scalar=thread),
                _Result(scalars=[]),
            ]
        )
    )

    response = await read_delta(
        db,
        thread_id=thread.id,
        creator_id=thread.creator_id,
        after_sequence=8,
        limit=20,
    )

    assert response.events == []
    assert response.next_after_sequence == 8
    assert response.has_more is False


@pytest.mark.asyncio
async def test_read_delta_hides_thread_from_non_owner() -> None:
    db = SimpleNamespace(execute=AsyncMock(return_value=_Result(scalar=None)))

    with pytest.raises(RuntimeFailure) as failure:
        await read_delta(
            db,
            thread_id=uuid.uuid4(),
            creator_id=uuid.uuid4(),
            after_sequence=-1,
            limit=20,
        )

    assert failure.value.status_code == 404
    assert failure.value.code == "thread_not_found"


@pytest.mark.asyncio
async def test_submit_turn_replays_same_digest_without_second_event() -> None:
    thread = _thread(revision=12)
    body = SubmitTurnBody(
        message="Open with the whisking close-up",
        client_event_id="phone-1:message-2",
        expected_thread_revision=11,
    )
    existing = SimpleNamespace(
        id=uuid.uuid4(),
        request_digest=request_digest(body),
        status="pending",
    )
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[_Result(scalar=thread), _Result(scalar=existing)]),
        rollback=AsyncMock(),
    )

    response, should_publish = await submit_turn(
        db,
        thread_id=thread.id,
        creator_id=thread.creator_id,
        body=body,
    )

    assert response.replayed is True
    assert response.turn_id == str(existing.id)
    assert should_publish is True
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_submit_turn_rejects_same_key_with_different_body() -> None:
    thread = _thread(revision=12)
    body = SubmitTurnBody(
        message="Remove the hotel clip",
        client_event_id="phone-1:message-2",
        expected_thread_revision=12,
    )
    existing = SimpleNamespace(id=uuid.uuid4(), request_digest="different", status="completed")
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[_Result(scalar=thread), _Result(scalar=existing)]),
    )

    with pytest.raises(RuntimeFailure, match="message identity") as failure:
        await submit_turn(
            db,
            thread_id=thread.id,
            creator_id=thread.creator_id,
            body=body,
        )

    assert failure.value.code == "idempotency_key_reused"


@pytest.mark.asyncio
async def test_status_turn_completes_immediately_without_consuming_successor_slot() -> None:
    thread = _thread(revision=12)
    active = SimpleNamespace(id=uuid.uuid4(), status="observing")
    body = SubmitTurnBody(
        message="How is it going?",
        client_event_id="phone-1:status-1",
        expected_thread_revision=12,
    )
    added: list[object] = []

    async def _flush() -> None:
        for value in added:
            if getattr(value, "id", None) is None:
                value.id = uuid.uuid4()

    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _Result(scalar=thread),
                _Result(scalar=None),
                _Result(scalars=[active]),
                _Result(scalar=4),
                _Result(scalar=5),
            ]
        ),
        add=MagicMock(side_effect=added.append),
        flush=AsyncMock(side_effect=_flush),
        commit=AsyncMock(),
    )

    response, should_publish = await submit_turn(
        db,
        thread_id=thread.id,
        creator_id=thread.creator_id,
        body=body,
    )

    assert response.status == "completed"
    assert response.thread_revision == 14
    assert should_publish is False
    assert len(added) == 3
    assert added[1].content == "Your approved render is in progress. Your draft is saved."
    assert added[2].status == "completed"
    assert added[2].queued_replaces_turn_id is None


@pytest.mark.parametrize(
    ("message", "status", "help_request"),
    [
        ("Status update", True, False),
        ("What can you do?", False, True),
        ("Help me make this faster", False, False),
    ],
)
def test_inert_question_classifier_does_not_capture_edit_requests(
    message: str,
    status: bool,
    help_request: bool,
) -> None:
    assert is_status_question(message) is status
    assert is_help_question(message) is help_request


@pytest.mark.asyncio
async def test_second_turn_is_one_unpublished_queued_successor() -> None:
    thread = _thread(revision=12)
    active = SimpleNamespace(id=uuid.uuid4(), status="planning")
    body = SubmitTurnBody(
        message="Use quieter music after this version",
        client_event_id="phone-1:message-3",
        expected_thread_revision=12,
    )
    added: list[object] = []

    async def _flush() -> None:
        for value in added:
            if getattr(value, "id", None) is None:
                value.id = uuid.uuid4()

    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _Result(scalar=thread),
                _Result(scalar=None),
                _Result(scalars=[active]),
                _Result(scalar=None),
                _Result(scalar=4),
            ]
        ),
        add=MagicMock(side_effect=added.append),
        flush=AsyncMock(side_effect=_flush),
        commit=AsyncMock(),
    )

    response, should_publish = await submit_turn(
        db,
        thread_id=thread.id,
        creator_id=thread.creator_id,
        body=body,
    )

    queued = added[-1]
    event = added[-2]
    assert queued.status == "queued"
    assert queued.queued_replaces_turn_id == active.id
    assert response.status == "queued"
    assert should_publish is False
    assert event.payload["turn_id"] == str(queued.id)
    assert event.payload["turn_status"] == "queued"


@pytest.mark.asyncio
async def test_submit_rejects_during_successor_promotion_window() -> None:
    thread = _thread(revision=12)
    queued = SimpleNamespace(id=uuid.uuid4(), status="queued")
    body = SubmitTurnBody(
        message="Make the next version calmer",
        client_event_id="phone-1:message-4",
        expected_thread_revision=12,
    )
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _Result(scalar=thread),
                _Result(scalar=None),
                _Result(scalars=[]),
                _Result(scalar=queued),
            ]
        )
    )

    with pytest.raises(RuntimeFailure) as failure:
        await submit_turn(
            db,
            thread_id=thread.id,
            creator_id=thread.creator_id,
            body=body,
        )

    assert failure.value.code == "queued_successor_exists"


@pytest.mark.asyncio
async def test_cancel_turn_rejects_stale_thread_revision_before_mutation() -> None:
    thread = _thread(revision=8)
    turn = SimpleNamespace(id=uuid.uuid4(), thread_id=thread.id, status="planning")
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[_Result(scalar=turn), _Result(scalar=thread)]),
        commit=AsyncMock(),
    )

    with pytest.raises(RuntimeFailure) as failure:
        await cancel_turn(
            db,
            thread_id=thread.id,
            turn_id=turn.id,
            creator_id=thread.creator_id,
            expected_thread_revision=7,
        )

    assert failure.value.code == "thread_revision_stale"
    assert failure.value.current_revision == 8
    assert turn.status == "planning"
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_turn_rejects_finished_turn() -> None:
    thread = _thread(revision=8)
    turn = SimpleNamespace(id=uuid.uuid4(), thread_id=thread.id, status="completed")
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[_Result(scalar=turn), _Result(scalar=thread)]),
        commit=AsyncMock(),
    )

    with pytest.raises(RuntimeFailure) as failure:
        await cancel_turn(
            db,
            thread_id=thread.id,
            turn_id=turn.id,
            creator_id=thread.creator_id,
            expected_thread_revision=8,
        )

    assert failure.value.code == "turn_not_cancellable"
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_turn_replays_already_cancelled_terminal_state() -> None:
    thread = _thread(revision=8)
    turn = SimpleNamespace(id=uuid.uuid4(), thread_id=thread.id, status="cancelled")
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[_Result(scalar=turn), _Result(scalar=thread)]),
        commit=AsyncMock(),
    )

    response, successor_id = await cancel_turn(
        db,
        thread_id=thread.id,
        turn_id=turn.id,
        creator_id=thread.creator_id,
        expected_thread_revision=8,
    )

    assert response.status == "cancelled"
    assert response.thread_revision == 8
    assert successor_id is None
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_awaiting_approval_cancels_all_pending_approvals() -> None:
    thread = _thread(revision=8)
    turn = SimpleNamespace(
        id=uuid.uuid4(),
        thread_id=thread.id,
        status="awaiting_approval",
        cancel_requested_at=None,
        completed_at=None,
    )
    approvals = [
        SimpleNamespace(id=uuid.uuid4(), status="pending"),
        SimpleNamespace(id=uuid.uuid4(), status="pending"),
    ]
    added: list[object] = []
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _Result(scalar=turn),
                _Result(scalar=thread),
                _Result(scalars=approvals),
                _Result(scalar=11),
                _Result(scalars=[]),
            ]
        ),
        add=MagicMock(side_effect=added.append),
        flush=AsyncMock(),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )

    response, successor_id = await cancel_turn(
        db,
        thread_id=thread.id,
        turn_id=turn.id,
        creator_id=thread.creator_id,
        expected_thread_revision=8,
    )

    assert [approval.status for approval in approvals] == ["cancelled", "cancelled"]
    assert response.approval_ids == [str(approval.id) for approval in approvals]
    assert turn.status == "cancelled"
    assert turn.cancel_requested_at is not None
    assert response.thread_revision == 9
    assert successor_id is None
    assert added[-1].event_type == "turn_cancelled"
    assert added[-1].payload["approval_ids"] == response.approval_ids
    db.commit.assert_awaited_once()
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_turn_promotes_one_queued_successor_after_commit() -> None:
    thread = _thread(revision=8)
    turn = SimpleNamespace(
        id=uuid.uuid4(),
        thread_id=thread.id,
        status="planning",
        cancel_requested_at=None,
        completed_at=None,
    )
    successor = SimpleNamespace(id=uuid.uuid4(), status="queued")
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _Result(scalar=turn),
                _Result(scalar=thread),
                _Result(scalar=11),
                _Result(scalars=[successor]),
                _Result(scalar=thread),
            ]
        ),
        add=MagicMock(),
        flush=AsyncMock(),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )

    response, successor_id = await cancel_turn(
        db,
        thread_id=thread.id,
        turn_id=turn.id,
        creator_id=thread.creator_id,
        expected_thread_revision=8,
    )

    assert response.status == "cancelled"
    assert successor.status == "pending"
    assert successor_id == str(successor.id)
    assert db.commit.await_count == 2
    db.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_approve_records_consent_but_never_dispatches_render() -> None:
    thread = _thread(revision=5)
    draft = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=thread.creator_id,
        thread_id=thread.id,
        draft_revision=9,
        is_head=True,
    )
    approval = _approval(thread=thread, draft=draft, creator_id=thread.creator_id)
    thread.active_creator_agent_session_id = approval.session_id
    session = _session_for(approval)
    turn = _turn_for(approval)
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _Result(scalar=approval),
                _Result(scalar=session),
                _Result(scalar=turn),
                _Result(scalar=draft),
                _Result(scalar=approval),
                _Result(scalar=thread),
                _Result(scalar=21),
            ]
        ),
        add=MagicMock(),
        flush=AsyncMock(),
        commit=AsyncMock(),
    )
    body = ApprovalDecisionBody(
        expected_thread_revision=5,
        expected_draft_revision=9,
        expected_approval_fingerprint=approval_fingerprint(approval),
    )

    with patch("app.services.job_dispatch.enqueue_orchestrator") as dispatch:
        response, successor_id = await decide_approval(
            db,
            thread_id=thread.id,
            approval_id=approval.id,
            creator_id=thread.creator_id,
            decision="approve",
            body=body,
        )

    assert approval.status == "approved"
    assert response.render_dispatched is False
    assert successor_id is None
    assert response.thread_revision == 6
    dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_approve_rejects_changed_draft_head() -> None:
    thread = _thread(revision=5)
    draft = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=thread.creator_id,
        thread_id=thread.id,
        draft_revision=10,
        is_head=True,
    )
    approval = _approval(thread=thread, draft=draft, creator_id=thread.creator_id)
    thread.active_creator_agent_session_id = approval.session_id
    session = _session_for(approval)
    turn = _turn_for(approval)
    approval.draft_revision = 9
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _Result(scalar=approval),
                _Result(scalar=session),
                _Result(scalar=turn),
                _Result(scalar=draft),
                _Result(scalar=approval),
                _Result(scalar=thread),
            ]
        )
    )
    body = ApprovalDecisionBody(
        expected_thread_revision=5,
        expected_draft_revision=9,
        expected_approval_fingerprint=approval_fingerprint(approval),
    )

    with pytest.raises(RuntimeFailure) as failure:
        await decide_approval(
            db,
            thread_id=thread.id,
            approval_id=approval.id,
            creator_id=thread.creator_id,
            decision="approve",
            body=body,
        )

    assert failure.value.code == "draft_stale"


@pytest.mark.asyncio
async def test_approve_rejects_changed_session_pin() -> None:
    thread = _thread(revision=5)
    draft = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=thread.creator_id,
        thread_id=thread.id,
        draft_revision=9,
        is_head=True,
    )
    approval = _approval(thread=thread, draft=draft, creator_id=thread.creator_id)
    thread.active_creator_agent_session_id = approval.session_id
    session = _session_for(approval)
    turn = _turn_for(approval)
    session.ownership_epoch += 1
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _Result(scalar=approval),
                _Result(scalar=session),
                _Result(scalar=turn),
                _Result(scalar=draft),
                _Result(scalar=approval),
                _Result(scalar=thread),
            ]
        )
    )
    body = ApprovalDecisionBody(
        expected_thread_revision=5,
        expected_draft_revision=9,
        expected_approval_fingerprint=approval_fingerprint(approval),
    )

    with pytest.raises(RuntimeFailure) as failure:
        await decide_approval(
            db,
            thread_id=thread.id,
            approval_id=approval.id,
            creator_id=thread.creator_id,
            decision="approve",
            body=body,
        )

    assert failure.value.code == "approval_target_stale"


@pytest.mark.asyncio
async def test_deny_approval_completes_turn_and_promotes_successor() -> None:
    thread = _thread(revision=5)
    draft = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=thread.creator_id,
        thread_id=thread.id,
        draft_revision=9,
        is_head=True,
    )
    approval = _approval(thread=thread, draft=draft, creator_id=thread.creator_id)
    thread.active_creator_agent_session_id = approval.session_id
    session = _session_for(approval)
    turn = _turn_for(approval)
    successor = SimpleNamespace(id=uuid.uuid4(), status="queued")
    added: list[object] = []
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _Result(scalar=approval),
                _Result(scalar=session),
                _Result(scalar=turn),
                _Result(scalar=draft),
                _Result(scalar=approval),
                _Result(scalar=thread),
                _Result(scalar=21),
                _Result(scalars=[successor]),
                _Result(scalar=thread),
            ]
        ),
        add=MagicMock(side_effect=added.append),
        flush=AsyncMock(),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )

    response, successor_id = await decide_approval(
        db,
        thread_id=thread.id,
        approval_id=approval.id,
        creator_id=thread.creator_id,
        decision="deny",
        body=_approval_body(approval, thread_revision=5, draft_revision=9),
    )

    assert approval.status == "denied"
    assert turn.status == "completed"
    assert turn.completed_at is not None
    assert response.status == "denied"
    assert response.render_dispatched is False
    assert successor.status == "pending"
    assert successor_id == str(successor.id)
    assert added[-1].event_type == "approval_denied"
    assert added[-1].payload["render_dispatched"] is False
    assert db.commit.await_count == 2


@pytest.mark.asyncio
async def test_approval_expiry_is_persisted_before_rejection() -> None:
    thread = _thread(revision=5)
    draft = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=thread.creator_id,
        thread_id=thread.id,
        draft_revision=9,
        is_head=True,
    )
    approval = _approval(thread=thread, draft=draft, creator_id=thread.creator_id)
    approval.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    thread.active_creator_agent_session_id = approval.session_id
    session = _session_for(approval)
    turn = _turn_for(approval)
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _Result(scalar=approval),
                _Result(scalar=session),
                _Result(scalar=turn),
                _Result(scalar=draft),
                _Result(scalar=approval),
                _Result(scalar=thread),
            ]
        ),
        commit=AsyncMock(),
    )

    with pytest.raises(RuntimeFailure) as failure:
        await decide_approval(
            db,
            thread_id=thread.id,
            approval_id=approval.id,
            creator_id=thread.creator_id,
            decision="approve",
            body=_approval_body(approval, thread_revision=5, draft_revision=9),
        )

    assert failure.value.code == "approval_expired"
    assert approval.status == "expired"
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_approval_rejects_mismatched_fingerprint_without_mutation() -> None:
    thread = _thread(revision=5)
    draft = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=thread.creator_id,
        thread_id=thread.id,
        draft_revision=9,
        is_head=True,
    )
    approval = _approval(thread=thread, draft=draft, creator_id=thread.creator_id)
    thread.active_creator_agent_session_id = approval.session_id
    session = _session_for(approval)
    turn = _turn_for(approval)
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _Result(scalar=approval),
                _Result(scalar=session),
                _Result(scalar=turn),
                _Result(scalar=draft),
                _Result(scalar=approval),
                _Result(scalar=thread),
            ]
        ),
        commit=AsyncMock(),
    )
    body = ApprovalDecisionBody(
        expected_thread_revision=5,
        expected_draft_revision=9,
        expected_approval_fingerprint="0" * 64,
    )

    with pytest.raises(RuntimeFailure) as failure:
        await decide_approval(
            db,
            thread_id=thread.id,
            approval_id=approval.id,
            creator_id=thread.creator_id,
            decision="approve",
            body=body,
        )

    assert failure.value.code == "approval_stale"
    assert approval.status == "pending"
    assert turn.status == "awaiting_approval"
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_approval_rejects_non_pending_record_without_replaying_decision() -> None:
    thread = _thread(revision=5)
    draft = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=thread.creator_id,
        thread_id=thread.id,
        draft_revision=9,
        is_head=True,
    )
    approval = _approval(thread=thread, draft=draft, creator_id=thread.creator_id)
    approval.status = "approved"
    thread.active_creator_agent_session_id = approval.session_id
    session = _session_for(approval)
    turn = _turn_for(approval)
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _Result(scalar=approval),
                _Result(scalar=session),
                _Result(scalar=turn),
                _Result(scalar=draft),
                _Result(scalar=approval),
                _Result(scalar=thread),
            ]
        ),
        commit=AsyncMock(),
    )

    with pytest.raises(RuntimeFailure) as failure:
        await decide_approval(
            db,
            thread_id=thread.id,
            approval_id=approval.id,
            creator_id=thread.creator_id,
            decision="approve",
            body=_approval_body(approval, thread_revision=5, draft_revision=9),
        )

    assert failure.value.code == "approval_not_pending"
    assert approval.status == "approved"
    db.commit.assert_not_awaited()


def test_runtime_snapshot_uses_opaque_media_labels_only() -> None:
    thread = SimpleNamespace(
        state={
            "media": [
                {"media_id": "media-1", "filename": "whisking.mov", "gcs_path": "secret/path"}
            ],
            "edit_format": "day_vlog",
        }
    )

    snapshot = _snapshot(thread)

    assert snapshot["media_labels"] == ["whisking.mov"]
    assert "secret/path" not in str(snapshot)


def test_successor_promotion_never_nests_queued_turn_lock_under_thread_lock() -> None:
    """Pin the deadlock fix: complete/cancel release Thread before Turn B."""

    complete_source = inspect.getsource(_complete_read_turn)
    cancel_source = inspect.getsource(cancel_turn)

    assert complete_source.index("db.commit()") < complete_source.index(
        "_promote_queued_successor_sync"
    )
    assert cancel_source.index("await db.commit()") < cancel_source.index(
        "_promote_queued_successor"
    )


def test_completion_requires_exact_live_lease_epoch() -> None:
    now = datetime.now(UTC)
    turn = SimpleNamespace(
        id=uuid.uuid4(),
        status="planning",
        lease_owner="same-celery-task-id",
        lease_epoch=2,
        lease_expires_at=now + timedelta(seconds=5),
    )

    assert _owns_turn_lease(
        turn, lease_owner="same-celery-task-id", lease_epoch=2, database_now=now
    )
    assert not _owns_turn_lease(
        turn, lease_owner="same-celery-task-id", lease_epoch=1, database_now=now
    )
    assert not _owns_turn_lease(
        turn,
        lease_owner="same-celery-task-id",
        lease_epoch=2,
        database_now=now + timedelta(seconds=6),
    )


def test_lease_renewal_uses_database_time_and_exact_epoch() -> None:
    now = datetime.now(UTC)
    turn = SimpleNamespace(
        id=uuid.uuid4(),
        status="planning",
        lease_owner="worker-1",
        lease_epoch=3,
        lease_expires_at=now + timedelta(seconds=5),
    )

    class _Db:
        def __init__(self) -> None:
            self.commit = MagicMock()
            self.rollback = MagicMock()
            self.execute = MagicMock(side_effect=[_Result(scalar=turn), _Result(scalar=now)])

    database = _Db()

    @contextmanager
    def _session():
        yield database

    with patch("app.tasks.kria_runtime.sync_session", _session):
        assert _renew_turn_lease(turn.id, lease_owner="worker-1", lease_epoch=3)

    assert turn.lease_expires_at == now + timedelta(seconds=15)
    database.commit.assert_called_once()
    database.rollback.assert_not_called()


@pytest.mark.asyncio
async def test_live_planner_heartbeats_while_inference_is_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renewals: list[uuid.UUID] = []
    planned = PlannedKriaTurn(
        KriaTurnPlan(mode="respond", turn_value="question", response="Which shot should open?"),
        "manifest",
        "context",
    )

    class _AsyncContext:
        async def __aenter__(self):  # noqa: ANN204
            return SimpleNamespace()

        async def __aexit__(self, *_args):  # noqa: ANN002, ANN204
            return None

    async def _slow_plan(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        await asyncio.sleep(0.035)
        return planned

    def _renew(turn_id: uuid.UUID, **_kwargs) -> bool:  # noqa: ANN003
        renewals.append(turn_id)
        return True

    monkeypatch.setattr("app.tasks.kria_runtime.AsyncSessionLocal", lambda: _AsyncContext())
    monkeypatch.setattr("app.tasks.kria_runtime.plan_live_turn", _slow_plan)
    monkeypatch.setattr("app.tasks.kria_runtime._renew_turn_lease", _renew)
    monkeypatch.setattr("app.tasks.kria_runtime._LEASE_HEARTBEAT_SECONDS", 0.01)
    turn_id = uuid.uuid4()
    result = await _plan_with_live_agent(
        {"thread_id": uuid.uuid4(), "item_id": uuid.uuid4(), "creator_id": uuid.uuid4()},
        "Make it faster",
        turn_id=turn_id,
        lease_owner="worker-1",
        lease_epoch=2,
    )

    assert result == planned
    assert len(renewals) >= 2
    assert set(renewals) == {turn_id}


def test_live_plan_guard_replaces_paraphrase_only_response_and_action_summary() -> None:
    response = PlannedKriaTurn(
        KriaTurnPlan(
            mode="respond",
            turn_value="question",
            response="You want a fast matcha launch video.",
        ),
        "manifest",
        "context",
    )
    guarded_response = _useful_plan(response, user_message="Make a fast matcha launch video")
    assert guarded_response.plan.response != response.plan.response
    assert guarded_response.plan.turn_value == "question"

    action = PlannedKriaTurn(
        KriaTurnPlan(
            mode="act",
            turn_value="action",
            intents=[
                {
                    "intent_id": "apply-strategy",
                    "tool_name": "draft.apply_strategy",
                    "tool_version": 1,
                    "arguments": {
                        "summary": "You want a fast matcha launch video.",
                        "strategy": {"pacing": "fast", "edit_format": "day_vlog"},
                    },
                }
            ],
        ),
        "manifest",
        "context",
    )
    guarded_action = _useful_plan(action, user_message="Make a fast matcha launch video")
    assert (
        guarded_action.plan.intents[0]
        .arguments["summary"]
        .startswith("I prepared a fast day vlog draft")
    )


def test_failed_turn_publishes_its_promoted_successor() -> None:
    turn_id = str(uuid.uuid4())
    successor_id = str(uuid.uuid4())
    with (
        patch("app.tasks.kria_runtime._claim", return_value=({}, "make it faster", 3, 8)),
        patch("app.tasks.kria_runtime.KRIA_TOOLS.get", side_effect=ValueError("bad tool")),
        patch("app.tasks.kria_runtime._fail_turn", return_value=successor_id),
        patch.object(run_kria_turn, "apply_async") as publish,
        pytest.raises(ValueError, match="bad tool"),
    ):
        run_kria_turn.run(turn_id)

    publish.assert_called_once_with(
        args=[successor_id], task_id=successor_id, queue="agent-control"
    )
