from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.tasks import template_orchestrate


@pytest.mark.parametrize("status", ["processing", "template_ready", "processing_failed"])
def test_first_delivery_skips_job_that_is_no_longer_queued(status: str) -> None:
    job = SimpleNamespace(status=status)
    db = MagicMock()
    db.get.return_value = job

    claimed = template_orchestrate._claim_template_job_start(
        db,
        "12345678-1234-5678-1234-567812345678",
        operation="template_job_start",
        allow_processing_retry=False,
    )

    assert claimed is None


def test_first_delivery_claims_queued_job() -> None:
    job = SimpleNamespace(status="queued")
    db = MagicMock()
    db.get.return_value = job

    claimed = template_orchestrate._claim_template_job_start(
        db,
        "12345678-1234-5678-1234-567812345678",
        operation="template_job_start",
        allow_processing_retry=False,
    )

    assert claimed is job


@pytest.mark.parametrize(
    ("delivery_request", "expected"),
    [
        (SimpleNamespace(retries=1, delivery_info={}), True),
        (SimpleNamespace(retries=0, delivery_info={"redelivered": True}), True),
        (SimpleNamespace(retries=0, delivery_info={"redelivered": False}), False),
    ],
)
def test_processing_reentry_requires_retry_or_redelivery(
    delivery_request,  # noqa: ANN001
    expected: bool,
) -> None:
    assert template_orchestrate._request_allows_processing_retry(delivery_request) is expected

    job = SimpleNamespace(status="processing")
    db = MagicMock()
    db.get.return_value = job
    claimed = template_orchestrate._claim_template_job_start(
        db,
        "12345678-1234-5678-1234-567812345678",
        operation="template_job_start",
        allow_processing_retry=expected,
    )
    assert (claimed is job) is expected
