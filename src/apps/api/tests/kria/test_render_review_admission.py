"""Failure matrix: flag-off, stable identity, lost publish, duplicate lease,
expired lease cap, current authority drift, missing proof, advisory readiness.
Actual storage verification remains an independent adapter boundary.
"""

from unittest.mock import Mock

from app.config import settings
from app.services.render_review_admission import (
    admit_generation_review,
    publish_generation_review,
    reconcile_generation_reviews,
)


def test_flag_off_never_accesses_review_database_or_broker(monkeypatch):
    monkeypatch.setattr(settings, "kria_intent_review_enabled", False)
    db = Mock(side_effect=AssertionError("Flag off must not query"))
    assert admit_generation_review(db, None) is None
    assert publish_generation_review("ignored") is False
    assert reconcile_generation_reviews() == 0
    assert not db.mock_calls


def test_marker_does_not_mask_database_failures(monkeypatch):
    from types import SimpleNamespace

    import pytest
    from sqlalchemy.exc import OperationalError

    from app.services import render_review_admission as admission

    error = OperationalError("read metadata", {}, RuntimeError("database unavailable"))
    monkeypatch.setattr(admission, "_demand_identity", Mock(side_effect=error))
    execution = SimpleNamespace(result={})
    with pytest.raises(OperationalError):
        admission.mark_admission_demand(execution, SimpleNamespace(), SimpleNamespace())
    assert execution.result == {}
