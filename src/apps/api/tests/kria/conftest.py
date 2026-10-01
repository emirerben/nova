"""Shared fixtures for the Kria runtime tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _private_reconcile_lock_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reconcile sweep takes a database-wide advisory lock; xdist workers share one test
    database, so give each process its own key (a real overlap would skip the sweep)."""
    import os

    import app.tasks.kria_runtime as rt

    monkeypatch.setattr(rt, "_RECONCILE_ADVISORY_KEY", 0x4B52494100000000 + os.getpid())
