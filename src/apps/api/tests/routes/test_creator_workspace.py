"""Contract guards for approval-gated off-plan intake."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from app.agents.detect_plan_relevance import DetectPlanRelevanceAgent
from app.routes.creator_workspace import (
    WorkspaceCreateBody,
    WorkspaceDecisionBody,
    WorkspacePreferenceSignalBody,
    WorkspaceReceiptCreateBody,
    _enqueue_relevance_or_mark_failed,
    _media_paths_already_attached,
    _request_digest,
)


def test_workspace_upload_ids_are_opaque_and_idempotent() -> None:
    with pytest.raises(ValidationError):
        WorkspaceCreateBody(media_ids=["gs://bucket/raw.mp4"], idempotency_key="r1")
    with pytest.raises(ValidationError):
        WorkspaceCreateBody(media_ids=["clip-1", "clip-1"], idempotency_key="r1")
    one = _request_digest(uuid.uuid4(), 2, ["clip-1", "clip-2"])
    two = _request_digest(uuid.uuid4(), 2, ["clip-1", "clip-2"])
    assert one != two


def test_workspace_decision_requires_hash_and_explicit_action() -> None:
    decision = WorkspaceDecisionBody(
        expected_proposal_hash="a" * 64,
        decision="reject",
        client_event_id="event-1",
    )
    assert decision.decision == "reject"
    with pytest.raises(ValidationError):
        WorkspaceDecisionBody(
            expected_proposal_hash="A" * 64,
            decision="reject",
            client_event_id="event-1",
        )


def test_relevance_classifier_does_not_infer_preference_or_mutate_plan() -> None:
    result = DetectPlanRelevanceAgent().run(
        {
            "media": [{"media_id": "clip-1", "label": "market walk"}],
            "plan_items": [{"id": "item-1", "theme": "market day", "idea": "walk"}],
        }
    )
    assert result.relevance == "existing_item"
    assert result.target_plan_item_id == "item-1"
    assert result.topic is None

    fresh = DetectPlanRelevanceAgent().run(
        {
            "media": [{"media_id": "clip-2", "label": "sunset"}],
            "plan_items": [{"id": "item-1", "theme": "market day", "idea": "walk"}],
        }
    )
    assert fresh.relevance == "new_topic"
    assert fresh.topic == "New footage"


def test_workspace_receipt_pins_distinct_deliverables_and_rejects_duplicate_items() -> None:
    receipt = WorkspaceReceiptCreateBody(
        plan_item_ids=["item-1", "item-2"], idempotency_key="receipt-1"
    )
    assert receipt.plan_item_ids == ["item-1", "item-2"]
    with pytest.raises(ValidationError):
        WorkspaceReceiptCreateBody(plan_item_ids=["item-1", "item-1"], idempotency_key="receipt-1")
    with pytest.raises(ValidationError):
        WorkspaceReceiptCreateBody(
            plan_item_ids=["https://example.test/item"], idempotency_key="receipt-1"
        )


def test_workspace_preference_signal_is_creator_text_only() -> None:
    signal = WorkspacePreferenceSignalBody(
        note="  Please use a calmer text style.  ", client_event_id="event-1"
    )
    assert signal.note == "Please use a calmer text style."
    with pytest.raises(ValidationError):
        WorkspacePreferenceSignalBody(note="   ", client_event_id="event-2")
    with pytest.raises(ValidationError):
        WorkspacePreferenceSignalBody(
            note="Use a calmer style", client_event_id="event-3", inferred=True
        )


def test_workspace_rejects_cross_item_media_reuse() -> None:
    assert _media_paths_already_attached(
        [SimpleNamespace(clip_gcs_paths=["user/job-1/raw.mp4"], clip_assignments=[])],
        {"user/job-1/raw.mp4"},
    )
    assert not _media_paths_already_attached(
        [SimpleNamespace(clip_gcs_paths=["user/job-2/raw.mp4"], clip_assignments=[])],
        {"user/job-1/raw.mp4"},
    )


@pytest.mark.asyncio
async def test_workspace_queue_failure_is_visible(monkeypatch) -> None:
    row = SimpleNamespace(id=uuid.uuid4(), status="pending", error_code=None)
    db = SimpleNamespace(commit=AsyncMock())
    publish = Mock(side_effect=RuntimeError("broker unavailable"))
    monkeypatch.setattr("app.routes.creator_workspace.detect_plan_relevance.apply_async", publish)

    await _enqueue_relevance_or_mark_failed(db, row)

    assert row.status == "failed"
    assert row.error_code == "relevance_dispatch_failed"
    db.commit.assert_awaited_once()
