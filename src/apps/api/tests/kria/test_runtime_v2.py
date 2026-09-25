from __future__ import annotations

import asyncio
import inspect
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import ArgumentError
from sqlalchemy.pool import NullPool

from app.config import settings
from app.kria import drafts
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
    _ClaimsExhausted,
    _complete_read_turn,
    _Completion,
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
        title="Existing chat",
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
        client_event_id=f"client-{sequence}",
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


@pytest.mark.asyncio
async def test_draft_target_uses_thread_selected_variant_for_multi_variant_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    creator_id = uuid.uuid4()
    job_id = uuid.uuid4()
    item = SimpleNamespace(id=uuid.uuid4(), current_job_id=job_id)
    thread = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=creator_id,
        revision=8,
        active_plan_item_id=item.id,
        active_creator_agent_session_id=uuid.uuid4(),
        state={"selected_variant_id": " song_text "},
    )
    job = SimpleNamespace(
        id=job_id,
        assembly_plan={
            "variants": [
                {"variant_id": "original_text", "render_generation_id": "original-generation"},
                {"variant_id": "song_text", "render_generation_id": "song-generation"},
            ]
        },
    )
    session = SimpleNamespace(target_job_id=job_id, target_variant_id=None)
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[_Result(scalar=item), _Result(scalar=job)]),
        get=AsyncMock(return_value=session),
    )
    monkeypatch.setattr(drafts, "_owned_thread", AsyncMock(return_value=thread))

    target = await drafts._target(
        db,
        thread_id=thread.id,
        creator_id=creator_id,
        lock_item=True,
    )

    assert target.variant_key == "song_text"
    assert target.generation_id == "song-generation"


@pytest.mark.asyncio
async def test_draft_target_falls_back_to_active_session_when_thread_selection_is_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    creator_id = uuid.uuid4()
    job_id = uuid.uuid4()
    item = SimpleNamespace(id=uuid.uuid4(), current_job_id=job_id)
    thread = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=creator_id,
        revision=9,
        active_plan_item_id=item.id,
        active_creator_agent_session_id=uuid.uuid4(),
        state={"selected_variant_id": "removed_variant"},
    )
    job = SimpleNamespace(
        id=job_id,
        assembly_plan={
            "variants": [
                {"variant_id": "original_text", "render_generation_id": "original-generation"},
                {"variant_id": "song_text", "render_generation_id": "song-generation"},
            ]
        },
    )
    session = SimpleNamespace(target_job_id=job_id, target_variant_id="song_text")
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[_Result(scalar=item), _Result(scalar=job)]),
        get=AsyncMock(return_value=session),
    )
    monkeypatch.setattr(drafts, "_owned_thread", AsyncMock(return_value=thread))

    target = await drafts._target(
        db,
        thread_id=thread.id,
        creator_id=creator_id,
        lock_item=False,
    )

    assert target.variant_key == "song_text"
    assert target.generation_id == "song-generation"


@pytest.mark.asyncio
async def test_draft_target_rejects_ambiguous_multi_variant_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    creator_id = uuid.uuid4()
    job_id = uuid.uuid4()
    item = SimpleNamespace(id=uuid.uuid4(), current_job_id=job_id)
    thread = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=creator_id,
        revision=10,
        active_plan_item_id=item.id,
        active_creator_agent_session_id=None,
        state={"selected_variant_id": "removed_variant"},
    )
    job = SimpleNamespace(
        id=job_id,
        assembly_plan={
            "variants": [
                {"variant_id": "original_text"},
                {"variant_id": "song_text"},
            ]
        },
    )
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[_Result(scalar=item), _Result(scalar=job)]),
        get=AsyncMock(),
    )
    monkeypatch.setattr(drafts, "_owned_thread", AsyncMock(return_value=thread))

    with pytest.raises(RuntimeFailure) as raised:
        await drafts._target(
            db,
            thread_id=thread.id,
            creator_id=creator_id,
            lock_item=False,
        )

    assert raised.value.status_code == 409
    assert raised.value.code == "draft_variant_ambiguous"
    assert raised.value.recovery == "ask_user"


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
    assert [event.client_event_id for event in response.events] == ["client-5", "client-6"]
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


def _question_turn() -> PlannedKriaTurn:
    return PlannedKriaTurn(
        KriaTurnPlan(mode="respond", turn_value="question", response="Which shot should open?"),
        "manifest",
        "context",
    )


def _record_lease_renewals(monkeypatch: pytest.MonkeyPatch) -> list[uuid.UUID]:
    """Renew the turn lease every 10 ms and record each renewal."""

    renewals: list[uuid.UUID] = []

    def _renew(turn_id: uuid.UUID, **_kwargs) -> bool:  # noqa: ANN003
        renewals.append(turn_id)
        return True

    monkeypatch.setattr("app.tasks.kria_runtime._renew_turn_lease", _renew)
    monkeypatch.setattr("app.tasks.kria_runtime._LEASE_HEARTBEAT_SECONDS", 0.01)
    return renewals


async def _wait_until(condition, timeout: float = 5.0) -> None:  # noqa: ANN001
    """Keep "inference" running until the heartbeat has done its part: renewals
    run on a worker thread, so a fixed sleep races a loaded CI runner."""

    async def _poll() -> None:
        while not condition():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(_poll(), timeout)


@pytest.mark.asyncio
async def test_live_planner_heartbeats_while_inference_is_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renewals = _record_lease_renewals(monkeypatch)
    planned = _question_turn()

    async def _slow_plan(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        # Never touches the session, so the per-call engine never connects.
        await _wait_until(lambda: len(renewals) >= 2)
        return planned

    monkeypatch.setattr("app.tasks.kria_runtime.plan_live_turn", _slow_plan)
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


class _PlanningEngine:
    """Stands in for the per-call asyncpg engine and records its lifecycle."""

    def __init__(self, events: list[str], name: str) -> None:
        self.events = events
        self.name = name
        self.disposals = 0

    async def dispose(self) -> None:
        self.disposals += 1
        self.events.append(f"{self.name} disposed")


def _record_planning_engines(
    monkeypatch: pytest.MonkeyPatch, events: list[str]
) -> tuple[list[tuple[_PlanningEngine, str, dict]], list[dict]]:
    """Route `_plan_with_live_agent` onto recording engines and sessions."""

    engines: list[tuple[_PlanningEngine, str, dict]] = []
    sessionmakers: list[dict] = []

    def _create_engine(url: str, **kwargs) -> _PlanningEngine:  # noqa: ANN003
        engine = _PlanningEngine(events, name=f"engine-{len(engines)}")
        engines.append((engine, url, kwargs))
        events.append(f"{engine.name} created")
        return engine

    def _sessionmaker(engine: _PlanningEngine, **kwargs):  # noqa: ANN003, ANN202
        sessionmakers.append(kwargs)

        class _Session:
            async def __aenter__(self):  # noqa: ANN204
                events.append(f"{engine.name} session opened")
                return self

            async def __aexit__(self, *_exc):  # noqa: ANN002, ANN204
                events.append(f"{engine.name} session closed")
                return None

        return _Session

    monkeypatch.setattr("app.tasks.kria_runtime.create_async_engine", _create_engine)
    monkeypatch.setattr("app.tasks.kria_runtime.async_sessionmaker", _sessionmaker)
    return engines, sessionmakers


@pytest.mark.asyncio
@pytest.mark.parametrize("planning_fails", [False, True])
async def test_live_planner_closes_its_session_then_disposes_its_unpooled_engine(
    monkeypatch: pytest.MonkeyPatch,
    planning_fails: bool,
) -> None:
    """Each planning call owns an unpooled engine for its own event loop (prod
    turn 932db9f6 died reusing a pooled asyncpg connection from an earlier
    loop). Whether the Main Creator returns or raises, the session closes before
    the engine is disposed, and lease renewal stops with the call."""
    events: list[str] = []
    renewals = _record_lease_renewals(monkeypatch)
    engines, sessionmakers = _record_planning_engines(monkeypatch, events)
    planned = _question_turn()

    async def _plan(_db, **_kwargs):  # noqa: ANN001, ANN003, ANN202
        events.append("planning")
        await _wait_until(lambda: renewals)
        if planning_fails:
            raise RuntimeError("Kria could not produce a reliable editorial plan")
        return planned

    monkeypatch.setattr("app.tasks.kria_runtime.plan_live_turn", _plan)
    pending_before = asyncio.all_tasks()
    call = _plan_with_live_agent(
        {"thread_id": uuid.uuid4(), "item_id": uuid.uuid4(), "creator_id": uuid.uuid4()},
        "Make it faster",
        turn_id=uuid.uuid4(),
        lease_owner="worker-1",
        lease_epoch=2,
    )
    if planning_fails:
        with pytest.raises(RuntimeError, match="reliable editorial plan"):
            await call
    else:
        assert await call == planned
    renewals_at_exit = len(renewals)
    await asyncio.sleep(0.05)

    [(engine, url, engine_kwargs)] = engines
    assert url == settings.asyncpg_database_url
    assert engine_kwargs == {"poolclass": NullPool}
    assert sessionmakers == [{"expire_on_commit": False}]
    assert events == [
        "engine-0 created",
        "engine-0 session opened",
        "planning",
        "engine-0 session closed",
        "engine-0 disposed",
    ]
    assert engine.disposals == 1
    # The lease was renewed while the model ran, and renewal stopped with the call.
    assert renewals_at_exit >= 1
    assert len(renewals) == renewals_at_exit
    assert asyncio.all_tasks() <= pending_before


@pytest.mark.asyncio
async def test_live_planner_starts_no_heartbeat_when_its_engine_cannot_be_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The engine is built before the heartbeat task starts, so a bad database
    URL fails the turn without orphaning a task that keeps renewing its lease."""
    renewals = _record_lease_renewals(monkeypatch)
    plan = AsyncMock()

    def _unbuildable(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise ArgumentError("Could not parse SQLAlchemy URL")

    monkeypatch.setattr("app.tasks.kria_runtime.create_async_engine", _unbuildable)
    monkeypatch.setattr("app.tasks.kria_runtime.plan_live_turn", plan)
    pending_before = asyncio.all_tasks()

    with pytest.raises(ArgumentError, match="Could not parse"):
        await _plan_with_live_agent(
            {"thread_id": uuid.uuid4(), "item_id": uuid.uuid4(), "creator_id": uuid.uuid4()},
            "Make it faster",
            turn_id=uuid.uuid4(),
            lease_owner="worker-1",
            lease_epoch=2,
        )
    await asyncio.sleep(0.05)

    plan.assert_not_awaited()
    assert renewals == []
    assert asyncio.all_tasks() <= pending_before


@pytest.mark.asyncio
async def test_live_planner_keeps_renewing_after_a_failed_lease_renewal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One failed renewal (a database blip) is retried on the next tick: the
    lease must not lapse mid-plan, and the error must not replace the plan."""
    events: list[str] = []
    engines, _sessionmakers = _record_planning_engines(monkeypatch, events)
    renewals: list[uuid.UUID] = []
    planned = _question_turn()

    async def _plan(_db, **_kwargs):  # noqa: ANN001, ANN003, ANN202
        # Finish only once the heartbeat has renewed again after the failure; on
        # the old code the heartbeat died on it and this wait times out.
        await _wait_until(lambda: len(renewals) >= 2)
        return planned

    def _renew(turn_id: uuid.UUID, **_kwargs) -> bool:  # noqa: ANN003
        renewals.append(turn_id)
        if len(renewals) == 1:
            raise RuntimeError("lease store unavailable")
        return True

    monkeypatch.setattr("app.tasks.kria_runtime.plan_live_turn", _plan)
    monkeypatch.setattr("app.tasks.kria_runtime._renew_turn_lease", _renew)
    monkeypatch.setattr("app.tasks.kria_runtime._LEASE_HEARTBEAT_SECONDS", 0.01)

    result = await _plan_with_live_agent(
        {"thread_id": uuid.uuid4(), "item_id": uuid.uuid4(), "creator_id": uuid.uuid4()},
        "Make it faster",
        turn_id=uuid.uuid4(),
        lease_owner="worker-1",
        lease_epoch=2,
    )

    assert result == planned
    assert len(renewals) >= 2
    [(engine, _url, _kwargs)] = engines
    assert engine.disposals == 1


@pytest.mark.asyncio
async def test_live_planner_disposes_its_engine_when_the_heartbeat_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`asyncio.run` cancels every task at shutdown (a Celery soft time limit
    lands there), so awaiting the heartbeat can raise: the engine is still
    disposed on the loop that created it."""
    events: list[str] = []
    engines, _sessionmakers = _record_planning_engines(monkeypatch, events)
    _record_lease_renewals(monkeypatch)

    async def _plan(_db, **_kwargs):  # noqa: ANN001, ANN003, ANN202
        [heartbeat] = [
            task
            for task in asyncio.all_tasks()
            if getattr(task.get_coro(), "__name__", "") == "_heartbeat"
        ]
        heartbeat.cancel()
        await asyncio.sleep(0.02)
        return _question_turn()

    monkeypatch.setattr("app.tasks.kria_runtime.plan_live_turn", _plan)

    with pytest.raises(asyncio.CancelledError):
        await _plan_with_live_agent(
            {"thread_id": uuid.uuid4(), "item_id": uuid.uuid4(), "creator_id": uuid.uuid4()},
            "Make it faster",
            turn_id=uuid.uuid4(),
            lease_owner="worker-1",
            lease_epoch=2,
        )

    [(engine, _url, _kwargs)] = engines
    assert engine.disposals == 1
    assert events[-2:] == ["engine-0 session closed", "engine-0 disposed"]


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


def _live_turn_snapshot() -> dict[str, str]:
    return {
        "thread_id": str(uuid.uuid4()),
        "item_id": str(uuid.uuid4()),
        "creator_id": str(uuid.uuid4()),
    }


def test_consecutive_live_turns_in_one_worker_each_plan_on_their_own_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Celery child plans every turn in a fresh `asyncio.run` loop, so no
    engine (or connection) from one turn may survive into the next turn's loop.
    Unit twin of the real-Postgres regression in test_runtime_postgres_integration."""
    events: list[str] = []
    engines, _sessionmakers = _record_planning_engines(monkeypatch, events)
    loops: list[asyncio.AbstractEventLoop] = []
    planned = _question_turn()

    async def _plan(_db, **_kwargs):  # noqa: ANN001, ANN003, ANN202
        loops.append(asyncio.get_running_loop())
        events.append("planning")
        return planned

    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr("app.tasks.kria_runtime.plan_live_turn", _plan)
    with (
        patch(
            "app.tasks.kria_runtime._claim",
            side_effect=[
                (_live_turn_snapshot(), "Make it faster", 1, 4),
                (_live_turn_snapshot(), "Now open on the goal", 1, 6),
            ],
        ),
        patch(
            "app.tasks.kria_runtime._complete_response_turn",
            return_value=_Completion(committed=True),
        ) as complete,
        patch.object(run_kria_turn, "apply_async") as publish,
    ):
        results = [run_kria_turn.run(str(uuid.uuid4())) for _ in range(2)]

    assert [result["status"] for result in results] == ["completed", "completed"]
    assert complete.call_count == 2
    assert loops[0] is not loops[1]
    assert [engine.disposals for engine, _url, _kwargs in engines] == [1, 1]
    assert events == [
        "engine-0 created",
        "engine-0 session opened",
        "planning",
        "engine-0 session closed",
        "engine-0 disposed",
        "engine-1 created",
        "engine-1 session opened",
        "planning",
        "engine-1 session closed",
        "engine-1 disposed",
    ]
    publish.assert_not_called()


def test_live_planner_failure_is_projected_as_a_retryable_turn_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the Main Creator cannot plan (truncated, timed out, invalid output),
    the creator still gets the retryable "couldn't finish that step" projection
    under the claimed lease, after the planning engine has been released."""
    events: list[str] = []
    _record_planning_engines(monkeypatch, events)
    turn_id = str(uuid.uuid4())

    async def _plan(_db, **_kwargs):  # noqa: ANN001, ANN003, ANN202
        events.append("planning")
        raise RuntimeError("Kria could not produce a reliable editorial plan")

    def _fail(*_args, **_kwargs) -> None:  # noqa: ANN002, ANN003
        events.append("turn failed")

    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr("app.tasks.kria_runtime.plan_live_turn", _plan)
    with (
        patch(
            "app.tasks.kria_runtime._claim",
            return_value=(_live_turn_snapshot(), "Make it faster", 3, 8),
        ) as claim,
        patch("app.tasks.kria_runtime._fail_turn", side_effect=_fail) as fail,
        patch("app.tasks.kria_runtime._complete_response_turn") as complete_response,
        patch("app.tasks.kria_runtime._complete_draft_turn") as complete_draft,
        patch.object(run_kria_turn, "apply_async") as publish,
        pytest.raises(RuntimeError, match="reliable editorial plan"),
    ):
        run_kria_turn.run(turn_id)

    lease_owner = claim.call_args.args[1]
    fail.assert_called_once_with(
        uuid.UUID(turn_id),
        code="runtime_turn_failed",
        lease_owner=lease_owner,
        lease_epoch=3,
        # KRI-203: the failure carries its class + truncated message so an
        # operator can diagnose it from the admin turns/events reads.
        detail={
            "error_class": "RuntimeError",
            "error_message": "Kria could not produce a reliable editorial plan",
        },
    )
    assert events == [
        "engine-0 created",
        "engine-0 session opened",
        "planning",
        "engine-0 session closed",
        "engine-0 disposed",
        "turn failed",
    ]
    complete_response.assert_not_called()
    complete_draft.assert_not_called()
    publish.assert_not_called()


@pytest.mark.parametrize("successor", [None, "5c3f7d1e-3b1a-4f7e-9d2c-8a6b4e2f1c0d"])
def test_turn_out_of_claims_is_not_planned_and_publishes_its_successor(
    successor: str | None,
) -> None:
    turn_id = str(uuid.uuid4())
    with (
        patch("app.tasks.kria_runtime._claim", return_value=_ClaimsExhausted(successor)),
        patch("app.tasks.kria_runtime._plan_with_live_agent") as plan,
        patch.object(run_kria_turn, "apply_async") as publish,
    ):
        result = run_kria_turn.run(turn_id)

    assert result == {"turn_id": turn_id, "status": "failed"}
    plan.assert_not_called()
    if successor is None:
        publish.assert_not_called()
    else:
        publish.assert_called_once_with(args=[successor], task_id=successor, queue="agent-control")


@pytest.mark.asyncio
async def test_first_inert_prompt_also_reserves_title_generation():
    thread = _thread(revision=2)
    thread.title = "Untitled video"
    added = []

    async def flush():
        for row in added:
            if getattr(row, "id", None) is None:
                row.id = uuid.uuid4()

    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _Result(scalar=thread),
                _Result(scalar=None),
                _Result(scalars=[]),
                _Result(scalar=1),
                _Result(scalar=2),
            ]
        ),
        scalar=AsyncMock(return_value=None),
        add=MagicMock(side_effect=added.append),
        flush=AsyncMock(side_effect=flush),
        commit=AsyncMock(),
    )
    accepted, publish = await submit_turn(
        db,
        thread_id=thread.id,
        creator_id=thread.creator_id,
        body=SubmitTurnBody(
            message="What can you do?", client_event_id="first-help", expected_thread_revision=2
        ),
    )
    assert accepted.status == "completed"
    assert publish is False
    assert thread.title == "What can you do?"
    assert thread.state["title_generation"] == "pending"
    db.commit.assert_awaited_once()


def test_failure_detail_is_a_bounded_single_line_summary() -> None:
    from app.tasks.kria_runtime import _failure_detail

    class KriaEditorOpError(ValueError):
        pass

    detail = _failure_detail(KriaEditorOpError("A draft may contain\nat most eight " + "x" * 400))

    assert detail["error_class"] == "KriaEditorOpError"
    assert "\n" not in detail["error_message"]
    assert len(detail["error_message"]) == 200
    assert detail["error_message"].startswith("A draft may contain at most eight")
