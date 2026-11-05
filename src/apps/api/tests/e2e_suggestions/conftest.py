"""Shared setup for the end-to-end suggestion proof, and the writer of its report.

The database guard is the same CI-aware one the pinned-tap fixtures use: with no ``*_test``
Postgres a laptop run skips, under CI (``CI`` set; the api shards provide ``nova_test``) a missing
or unmigrated database fails collection, so a green check can never mean "none of it ran".
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.config import settings
from app.database import engine as async_engine
from app.database import sync_session


def _database_problem() -> str | None:
    name = make_url(settings.database_url).database or ""
    if not name.endswith("_test"):
        return f"refusing to write to non-test database {name!r}"
    try:
        with sync_session() as probe:
            probe.execute(text("select 1 from creative_brief_versions limit 1"))
    except Exception as exc:  # noqa: BLE001 - whatever is wrong, the fixtures cannot run
        return f"nova_test Postgres not reachable or not migrated ({type(exc).__name__})"
    return None


PROBLEM = _database_problem()
HERE = Path(__file__).resolve().parent


def pytest_collection_modifyitems(config, items):  # noqa: ANN001, ARG001
    """Skip this directory without a test database; under CI fail instead of skipping.

    Not a module-level ``pytest.skip``: raised from a conftest it would take the whole session down.
    """
    mine = [item for item in items if HERE in Path(str(item.fspath)).resolve().parents]
    if not mine or PROBLEM is None:
        return
    if os.environ.get("CI"):
        raise pytest.UsageError(f"{PROBLEM}: the end-to-end suggestion proof must run in CI")
    for item in mine:
        item.add_marker(pytest.mark.skip(reason=PROBLEM))


from tests.e2e_suggestions import fakes, report  # noqa: E402
from tests.tasks import kria_pinned_kinds_harness as k  # noqa: E402
from tests.tasks import kria_pinned_suggestion_harness as h  # noqa: E402


@pytest_asyncio.fixture(autouse=True)
async def _dispose_async_engine():
    yield
    await async_engine.dispose()


@pytest.fixture
def world(monkeypatch):
    """Every v2 flag on (the rollout configuration), every model and broker poisoned, and the
    suggestion store backed by an in-memory Redis. The mute kind keeps its shipped dark constant."""
    from app.services import suggestion_store as store

    w = h.install_world(monkeypatch)
    k.enable_kinds(monkeypatch)
    monkeypatch.setattr(settings, "kria_suggestion_editor_triggers_enabled", True)
    monkeypatch.setattr(settings, "silence_cut_enabled", True)
    # The runtime's crash-resume cache has a Redis of its own; it is best effort and not under test.
    monkeypatch.setattr("app.services.turn_checkpoint.discard", lambda _run_id: None)
    redis = fakes.FakeRedis()
    monkeypatch.setattr(store, "_redis", lambda: redis)
    w.redis = redis
    return w


@pytest.fixture
def talking_world(monkeypatch, world):
    """The speech-cleanup switches a Talking take needs (the capability the PATCH route checks)."""
    for name, value in k.CLEANUP_FLAGS.items():
        monkeypatch.setattr(settings, name, value)
    # A published preflight would reach the broker; the analysis row is already ready.
    monkeypatch.setattr(
        "app.services.plan_item_media.publish_preflight_after_commit", lambda _analysis_id: True
    )
    return world


def pytest_sessionfinish(session, exitstatus):  # noqa: ANN001, ARG001
    if not hasattr(session.config, "workerinput"):
        report.write_if_any()
