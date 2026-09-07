import importlib

import pytest

from app import models


def _foreign_key(table_name: str, column_name: str):
    column = models.Base.metadata.tables[table_name].c[column_name]
    return next(iter(column.foreign_keys))


def test_creator_memory_source_deletion_preserves_items_but_removes_raw_outbox() -> None:
    assert _foreign_key("creator_memory_items", "source_event_id").ondelete == "SET NULL"
    assert _foreign_key("creator_memory_outbox", "source_event_id").ondelete == "CASCADE"


def test_creator_memory_claim_and_foreign_key_indexes_are_declared() -> None:
    expected = {
        "idx_creator_memory_items_source_event",
        "idx_creator_memory_items_source_thread",
        "idx_creator_memory_operations_item",
        "idx_creator_memory_operations_source_event",
        "idx_creator_memory_outbox_pending_claim",
        "idx_creator_memory_outbox_leased_claim",
    }
    actual = {
        index.name
        for table_name in (
            "creator_memory_items",
            "creator_memory_operations",
            "creator_memory_outbox",
        )
        for index in models.Base.metadata.tables[table_name].indexes
    }
    assert expected <= actual


def test_creator_memory_migration_refuses_destructive_downgrade() -> None:
    migration = importlib.import_module("app.migrations.versions.0096_creator_memory_foundation")

    with pytest.raises(RuntimeError, match="intentionally data-preserving"):
        migration.downgrade()
