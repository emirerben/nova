from __future__ import annotations

import importlib
from pathlib import Path


def test_0074_upgrade_and_downgrade_are_symmetric(monkeypatch) -> None:
    migration = importlib.import_module(
        "app.migrations.versions.0074_plan_item_asset_analysis_state"
    )
    added: list[str] = []
    dropped: list[str] = []
    created_indexes: list[str] = []
    dropped_indexes: list[str] = []
    constraints: list[tuple[str, str]] = []
    executed = []

    monkeypatch.setattr(
        migration.op,
        "add_column",
        lambda _table, column: added.append(column.name),
    )
    monkeypatch.setattr(
        migration.op,
        "drop_column",
        lambda _table, name: dropped.append(name),
    )
    monkeypatch.setattr(
        migration.op,
        "create_index",
        lambda name, *_args, **_kwargs: created_indexes.append(name),
    )
    monkeypatch.setattr(
        migration.op,
        "drop_index",
        lambda name, **_kwargs: dropped_indexes.append(name),
    )
    monkeypatch.setattr(
        migration.op,
        "create_unique_constraint",
        lambda name, *_args, **_kwargs: constraints.append(("create", name)),
    )
    monkeypatch.setattr(
        migration.op,
        "drop_constraint",
        lambda name, *_args, **_kwargs: constraints.append(("drop", name)),
    )
    monkeypatch.setattr(migration.op, "execute", executed.append)

    migration.upgrade()
    migration.downgrade()

    assert set(added) == set(dropped)
    assert {
        "client_upload_id",
        "upload_content_type",
        "upload_size_bytes",
        "upload_expires_at",
        "gcs_generation",
        "correlation_id",
        "error_code",
        "analysis_attempt_token",
    } <= set(added)
    assert set(created_indexes) == set(dropped_indexes)
    assert constraints == [
        ("create", "uq_plan_item_assets_item_client_upload"),
        ("drop", "uq_plan_item_assets_item_client_upload"),
    ]
    assert len(executed) == 1
    assert "status = 'failed'" in str(executed[0])
    assert "error_retryable = true" in str(executed[0])


def test_alembic_commits_each_revision_independently() -> None:
    env_source = (Path(__file__).parents[1] / "app" / "migrations" / "env.py").read_text()

    assert env_source.count("transaction_per_migration=True") == 2


def test_0076_heif_recovery_index_is_symmetric(monkeypatch) -> None:
    migration = importlib.import_module("app.migrations.versions.0076_heif_recovery_index")
    created: list[tuple[str, str]] = []
    dropped: list[str] = []

    monkeypatch.setattr(
        migration.op,
        "create_index",
        lambda name, _table, _columns, **kwargs: created.append(
            (name, str(kwargs["postgresql_where"]))
        ),
    )
    monkeypatch.setattr(
        migration.op,
        "drop_index",
        lambda name, **_kwargs: dropped.append(name),
    )

    migration.upgrade()
    migration.downgrade()

    assert created == [
        (
            "idx_plan_item_assets_heif_unreadable_recovery",
            "status = 'failed' AND error_code = 'analysis_unreadable' "
            "AND upload_content_type IN ('image/heic', 'image/heif') "
            "AND analysis_attempt_count < 2",
        )
    ]
    assert dropped == ["idx_plan_item_assets_heif_unreadable_recovery"]
