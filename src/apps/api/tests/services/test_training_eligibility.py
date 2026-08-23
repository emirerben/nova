from __future__ import annotations

import importlib
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from app import models
from app.services.training_eligibility import (
    _decision,
    artifact_is_eligible,
    evaluate_artifact_eligibility,
)

migration = importlib.import_module("app.migrations.versions.0080_edit_feedback_learning_loop")


def test_learning_loop_migration_and_models_have_matching_tables() -> None:
    assert migration.revision == "0080"
    assert migration.down_revision == "0079"
    expected = {
        "internal_account_grants",
        "training_consent_events",
        "edit_artifacts",
        "edit_feedback_annotations",
        "training_artifact_retention_events",
        "training_dataset_exports",
    }
    assert expected <= set(models.Base.metadata.tables)
    assert {
        "storage_path",
        "storage_generation",
        "storage_content_hash",
        "render_receipt_hash",
        "direction_snapshot",
        "creator_split",
        "plan_item_split",
    } <= set(models.Base.metadata.tables["edit_artifacts"].columns.keys())
    annotation_constraints = {
        constraint.name
        for constraint in models.Base.metadata.tables["edit_feedback_annotations"].constraints
    }
    assert "ck_edit_feedback_annotations_dimension" in annotation_constraints


def test_internal_grant_wins_over_customer_consent() -> None:
    creator_id = uuid.uuid4()
    now = datetime.now(UTC)
    grant = SimpleNamespace(id=uuid.uuid4(), status="active", effective_at=now, created_at=now)
    consent = SimpleNamespace(id=uuid.uuid4(), action="grant", effective_at=now, created_at=now)
    decision = _decision(
        creator_id,
        internal_grant=grant,
        consent_event=consent,
        now=now,
    )
    assert decision.eligible is True
    assert decision.basis == "internal_grant"
    assert decision.internal_grant_id == grant.id
    assert decision.consent_event_id is None


def test_missing_or_revoked_grants_fail_closed() -> None:
    creator_id = uuid.uuid4()
    now = datetime.now(UTC)
    decision = _decision(creator_id, internal_grant=None, consent_event=None, now=now)
    assert decision.eligible is False
    assert decision.reason == "no_active_training_grant"

    revoked = SimpleNamespace(id=uuid.uuid4(), status="revoked", effective_at=now, created_at=now)
    decision = _decision(creator_id, internal_grant=revoked, consent_event=None, now=now)
    assert decision.eligible is False


def test_artifact_requires_exact_creator_prefix_and_current_basis(monkeypatch) -> None:
    creator_id = uuid.uuid4()
    artifact_id = uuid.uuid4()
    consent_id = uuid.uuid4()
    artifact = SimpleNamespace(
        id=artifact_id,
        creator_id=creator_id,
        artifact_kind="final_render",
        storage_path=f"users/{creator_id}/edit-feedback/{artifact_id}/final.mp4",
        storage_generation="42",
        storage_content_hash="sha256:abc",
        consent_event_id=consent_id,
        internal_grant_id=None,
    )
    monkeypatch.setattr(
        "app.services.training_eligibility.evaluate_training_eligibility",
        lambda *_args, **_kwargs: SimpleNamespace(
            eligible=True,
            basis="training_consent",
            consent_event_id=consent_id,
            internal_grant_id=None,
        ),
    )
    assert artifact_is_eligible(SimpleNamespace(), artifact) is True

    artifact.storage_path = "https://storage.example/final.mp4"
    decision = evaluate_artifact_eligibility(SimpleNamespace(), artifact)
    assert decision.eligible is False
    assert decision.reason == "artifact_storage_path_not_an_object_key"
