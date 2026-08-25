"""Contract guards for approval-gated off-plan intake."""

import uuid

import pytest
from pydantic import ValidationError

from app.agents.detect_plan_relevance import DetectPlanRelevanceAgent
from app.routes.creator_workspace import (
    WorkspaceCreateBody,
    WorkspaceDecisionBody,
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
