"""Keyed write fencing must not impose keyed requirements on legacy jobs."""

import uuid
from types import SimpleNamespace

import pytest

from app.services import slide_post_generation_delivery as delivery


def test_outside_keyed_context_does_not_read_job_or_database(monkeypatch):
    class LegacyJob:
        @property
        def id(self):
            pytest.fail("legacy job ID was read")

    monkeypatch.setattr(delivery, "_receipt_for_job", lambda *_: pytest.fail("SQL was read"))
    token = delivery.active_job.set(None)
    try:
        assert delivery.write_rejected(object(), LegacyJob()) is False
    finally:
        delivery.active_job.reset(token)


@pytest.mark.parametrize(
    "state,rejected", [(None, True), ("running", False), ("completed", True), ("failed", True)]
)
def test_claim_owned_write_requires_durable_running_receipt(monkeypatch, state, rejected):
    identifier = uuid.uuid4()
    calls = []

    def receipt(db, job_id):
        calls.append(job_id)
        return None if state is None else SimpleNamespace(execution_state=state)

    monkeypatch.setattr(delivery, "_receipt_for_job", receipt)
    token = delivery.active_job.set(identifier)
    try:
        assert delivery.write_rejected(object(), SimpleNamespace(id=identifier)) is rejected
        assert calls == [identifier]
    finally:
        delivery.active_job.reset(token)


def test_other_job_does_not_consume_claim_fence(monkeypatch):
    monkeypatch.setattr(delivery, "_receipt_for_job", lambda *_: pytest.fail("SQL was read"))
    token = delivery.active_job.set(uuid.uuid4())
    try:
        assert delivery.write_rejected(object(), SimpleNamespace(id=uuid.uuid4())) is False
    finally:
        delivery.active_job.reset(token)
