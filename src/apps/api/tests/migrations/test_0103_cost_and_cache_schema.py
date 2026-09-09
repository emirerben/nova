from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from app.config import settings
from app.database import sync_session


def _columns(table: str) -> set[str]:
    with sync_session() as db:
        return set(
            db.scalars(
                text(
                    """
                    SELECT column_name
                      FROM information_schema.columns
                     WHERE table_schema = current_schema()
                       AND table_name = :table
                    """
                ),
                {"table": table},
            )
        )


def test_0103_schema_matches_cost_and_owner_scoped_cache_models() -> None:
    db_name = make_url(settings.database_url).database or ""
    if not db_name.endswith("_test"):
        pytest.skip(f"refusing to inspect non-test database {db_name!r}")
    try:
        agent_run = _columns("agent_run")
    except OperationalError as exc:
        pytest.skip(f"Postgres not reachable for migration integration test: {exc!r}")

    reservations = _columns("ai_cost_reservations")
    media_cache = _columns("media_analysis_cache")
    reconciliation = _columns("billing_reconciliations")
    retention_entries = _columns("storage_retention_entries")

    assert "principal_type" not in agent_run
    assert "principal_type" in reservations
    assert "provider_started_at" in reservations
    assert "creator_id" in media_cache
    assert {"alert_claim_id", "alert_claim_expires_at"} <= reconciliation
    assert "deleted_at" in retention_entries
