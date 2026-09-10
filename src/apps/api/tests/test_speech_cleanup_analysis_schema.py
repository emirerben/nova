"""Structural contract for durable chat-first speech-cleanup analysis state.

These tests require neither storage nor a database. They lock the ORM/migration
shape that the preflight worker, current-detail projection, and reconcilers share.
"""

from __future__ import annotations

import importlib
import uuid
from datetime import UTC, datetime, timedelta

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint

from app import models

_PRIVATE_COLUMNS = {
    "source_media_identity",
    "source_storage_path",
    "source_generation",
    "source_policy_fingerprint",
    "attempt_token",
    "analysis_payload",
}

_BOUNDED_RESULT_COLUMNS = {
    "candidate_count",
    "category_counts",
    "estimated_removed_ms",
    "diagnostic_receipt",
    "failure_code",
    "failure_retryable",
}


def _index(name: str):
    table = models.SpeechCleanupAnalysis.__table__
    return next(index for index in table.indexes if index.name == name)


def _constraint_sql() -> dict[str, str]:
    table = models.SpeechCleanupAnalysis.__table__
    return {
        constraint.name: str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }


def test_schema_is_generation_pinned_and_separates_private_from_public_state() -> None:
    table = models.Base.metadata.tables["speech_cleanup_analyses"]

    assert {
        "id",
        "plan_item_id",
        "source_kind",
        "window_start_s",
        "window_end_s",
        "engine_version",
        "detector_version",
        "analysis_payload_version",
        "status",
        "attempt_count",
        "dispatched_at",
        "next_dispatch_at",
        "started_at",
        "lease_expires_at",
        "completed_at",
        "superseded_at",
        "decision",
        "decision_at",
        "created_at",
        "updated_at",
        *_PRIVATE_COLUMNS,
        *_BOUNDED_RESULT_COLUMNS,
    } <= set(table.columns.keys())
    assert models.PlanItem.__table__.columns["voiceover_generation"].nullable
    assert models.PlanItem.__table__.columns["voiceover_duration_s"].nullable

    now = datetime.now(UTC)
    private_payload = {
        "timed_words": [{"word": "um", "start_ms": 120, "end_ms": 310}],
        "safety_signals": {"mixed_gap": True},
        "cut_plan": [{"start_ms": 100, "end_ms": 340}],
        "diagnostics": {"silence_intervals": [[100, 340]]},
    }
    analysis = models.SpeechCleanupAnalysis(
        plan_item_id=uuid.uuid4(),
        source_kind="voiceover",
        source_media_identity="voiceover:media-1",
        source_storage_path="users/u/voiceovers/take.m4a",
        source_generation="1700000000000000",
        window_start_s=0.0,
        window_end_s=12.0,
        source_policy_fingerprint="sha256:source-policy",
        engine_version="speech-cleanup-v3",
        detector_version="mixed-gap-v2",
        analysis_payload_version="1",
        status="ready",
        attempt_token="attempt-1",
        attempt_count=1,
        started_at=now,
        lease_expires_at=now + timedelta(minutes=30),
        completed_at=now,
        analysis_payload=private_payload,
        candidate_count=1,
        category_counts={"filler": 1},
        estimated_removed_ms=240,
        diagnostic_receipt={"checked_duration_ms": 12_000},
    )

    assert analysis.analysis_payload == private_payload
    assert analysis.category_counts == {"filler": 1}
    assert _PRIVATE_COLUMNS.isdisjoint(_BOUNDED_RESULT_COLUMNS)


def test_lifecycle_source_decision_and_numeric_constraints_are_explicit() -> None:
    constraints = _constraint_sql()

    assert "'voiceover', 'embedded_spine'" in constraints["ck_speech_cleanup_analyses_source_kind"]
    assert (
        "'queued', 'running', 'ready', 'no_findings', 'failed'"
        in constraints["ck_speech_cleanup_analyses_status"]
    )
    decision_sql = constraints["ck_speech_cleanup_analyses_decision"]
    assert "'clean', 'keep_original', 'create_without_cleanup'" in decision_sql
    assert "decision IS NULL" in decision_sql
    assert "window_end_s > window_start_s" in constraints["ck_speech_cleanup_analyses_window"]
    assert "attempt_count >= 0" == constraints["ck_speech_cleanup_analyses_attempt_count"]
    assert "candidate_count >= 0" in constraints["ck_speech_cleanup_analyses_candidate_count"]
    assert "estimated_removed_ms >= 0" in constraints["ck_speech_cleanup_analyses_removed_ms"]
    assert "decision_at IS NOT NULL" in constraints["ck_speech_cleanup_analyses_decision_at"]


def test_identity_and_current_detail_indexes_are_unique_and_query_shaped() -> None:
    identity = _index("uq_speech_cleanup_analysis_identity")
    assert identity.unique
    assert [column.name for column in identity.columns] == [
        "plan_item_id",
        "source_policy_fingerprint",
        "engine_version",
    ]
    assert identity.dialect_options["postgresql"]["where"] is None

    current = _index("uq_speech_cleanup_analysis_current")
    assert current.unique
    assert [column.name for column in current.columns] == ["plan_item_id"]
    assert str(current.dialect_options["postgresql"]["where"]) == "superseded_at IS NULL"


def test_dispatch_and_lease_indexes_bound_reconciler_scans() -> None:
    dispatch = _index("idx_speech_cleanup_analysis_dispatch")
    assert not dispatch.unique
    assert [column.name for column in dispatch.columns] == [
        "next_dispatch_at",
        "created_at",
    ]
    assert str(dispatch.dialect_options["postgresql"]["where"]) == (
        "status = 'queued' AND dispatched_at IS NULL "
        "AND next_dispatch_at IS NOT NULL AND superseded_at IS NULL"
    )

    lease = _index("idx_speech_cleanup_analysis_lease")
    assert not lease.unique
    assert [column.name for column in lease.columns] == ["lease_expires_at", "id"]
    assert str(lease.dialect_options["postgresql"]["where"]) == (
        "status IN ('queued', 'running') AND lease_expires_at IS NOT NULL AND superseded_at IS NULL"
    )


def test_plan_item_analysis_relationship_is_cascading_and_bidirectional() -> None:
    plan_item_relationship = models.PlanItem.speech_cleanup_analyses.property
    analysis_relationship = models.SpeechCleanupAnalysis.plan_item.property

    assert plan_item_relationship.back_populates == "plan_item"
    assert "delete-orphan" in plan_item_relationship.cascade
    assert analysis_relationship.back_populates == "speech_cleanup_analyses"
    assert analysis_relationship.local_columns == {
        models.SpeechCleanupAnalysis.__table__.c.plan_item_id
    }


def test_0104_is_the_single_alembic_head() -> None:
    script_dir = ScriptDirectory.from_config(Config("alembic.ini"))
    assert script_dir.get_heads() == ["0104"]
    assert script_dir.get_revision("0094").down_revision == "0093"


def test_0094_upgrade_and_downgrade_are_symmetric(monkeypatch) -> None:
    migration = importlib.import_module("app.migrations.versions.0095_speech_cleanup_analysis")
    added_columns: list[tuple[str, str]] = []
    dropped_columns: list[tuple[str, str]] = []
    created_tables: list[tuple[str, set[str]]] = []
    dropped_tables: list[str] = []
    created_indexes: list[tuple[str, list[str], bool, str | None]] = []
    dropped_indexes: list[str] = []

    monkeypatch.setattr(
        migration.op,
        "add_column",
        lambda table, column: added_columns.append((table, column.name)),
    )
    monkeypatch.setattr(
        migration.op,
        "drop_column",
        lambda table, column: dropped_columns.append((table, column)),
    )

    def capture_table(name, *items):
        created_tables.append((name, {item.name for item in items if getattr(item, "name", None)}))

    monkeypatch.setattr(migration.op, "create_table", capture_table)
    monkeypatch.setattr(migration.op, "drop_table", dropped_tables.append)
    monkeypatch.setattr(
        migration.op,
        "create_index",
        lambda name, _table, columns, **kwargs: created_indexes.append(
            (
                name,
                list(columns),
                bool(kwargs.get("unique", False)),
                str(kwargs["postgresql_where"])
                if kwargs.get("postgresql_where") is not None
                else None,
            )
        ),
    )
    monkeypatch.setattr(
        migration.op,
        "drop_index",
        lambda name, **_kwargs: dropped_indexes.append(name),
    )

    migration.upgrade()
    migration.downgrade()

    assert added_columns == [
        ("plan_items", "voiceover_generation"),
        ("plan_items", "voiceover_duration_s"),
    ]
    assert dropped_columns == [
        ("plan_items", "voiceover_duration_s"),
        ("plan_items", "voiceover_generation"),
    ]
    assert created_tables == [
        (
            "speech_cleanup_analyses",
            {
                "id",
                "plan_item_id",
                "source_kind",
                "source_media_identity",
                "source_storage_path",
                "source_generation",
                "window_start_s",
                "window_end_s",
                "source_policy_fingerprint",
                "engine_version",
                "detector_version",
                "analysis_payload_version",
                "status",
                "attempt_token",
                "attempt_count",
                "dispatched_at",
                "next_dispatch_at",
                "started_at",
                "lease_expires_at",
                "completed_at",
                "superseded_at",
                "analysis_payload",
                "candidate_count",
                "category_counts",
                "estimated_removed_ms",
                "diagnostic_receipt",
                "failure_code",
                "failure_retryable",
                "decision",
                "decision_at",
                "created_at",
                "updated_at",
                "ck_speech_cleanup_analyses_source_kind",
                "ck_speech_cleanup_analyses_status",
                "ck_speech_cleanup_analyses_decision",
                "ck_speech_cleanup_analyses_window",
                "ck_speech_cleanup_analyses_attempt_count",
                "ck_speech_cleanup_analyses_candidate_count",
                "ck_speech_cleanup_analyses_removed_ms",
                "ck_speech_cleanup_analyses_decision_at",
            },
        )
    ]
    assert dropped_tables == ["speech_cleanup_analyses"]
    assert {name for name, *_rest in created_indexes} == set(dropped_indexes)

    indexes = {
        name: (columns, unique, predicate) for name, columns, unique, predicate in created_indexes
    }
    assert indexes["uq_speech_cleanup_analysis_identity"] == (
        ["plan_item_id", "source_policy_fingerprint", "engine_version"],
        True,
        None,
    )
    assert indexes["uq_speech_cleanup_analysis_current"] == (
        ["plan_item_id"],
        True,
        "superseded_at IS NULL",
    )
    assert indexes["idx_speech_cleanup_analysis_dispatch"][0] == [
        "next_dispatch_at",
        "created_at",
    ]
    assert indexes["idx_speech_cleanup_analysis_lease"][0] == ["lease_expires_at", "id"]
