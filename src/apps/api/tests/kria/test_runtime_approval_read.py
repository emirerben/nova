from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.kria.runtime import RuntimeFailure, approval_fingerprint, read_approval


class _Result:
    def __init__(self, value) -> None:  # noqa: ANN001
        self.value = value

    def scalar_one_or_none(self):  # noqa: ANN201
        return self.value


def _thread(creator_id: uuid.UUID) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=creator_id,
        runtime_version=2,
        status="active",
        revision=7,
    )


def _approval(thread_id: uuid.UUID, creator_id: uuid.UUID) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=creator_id,
        thread_id=thread_id,
        session_id=uuid.uuid4(),
        turn_id=uuid.uuid4(),
        draft_id=uuid.uuid4(),
        draft_revision=3,
        target_job_id=uuid.uuid4(),
        target_variant_id="tight-cut",
        target_generation_id="generation-4",
        target_manifest_hash="manifest-4",
        target_ownership_epoch=2,
        consequence_summary="Render the tighter matcha cut.",
        cost_summary="One render",
        status="pending",
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )


@pytest.mark.asyncio
async def test_read_approval_exposes_exact_server_fingerprint() -> None:
    creator_id = uuid.uuid4()
    thread = _thread(creator_id)
    approval = _approval(thread.id, creator_id)
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[_Result(thread), _Result(approval)]),
    )

    response = await read_approval(
        db,
        thread_id=thread.id,
        approval_id=approval.id,
        creator_id=creator_id,
    )

    assert response.approval_id == str(approval.id)
    assert response.draft_revision == 3
    assert response.status == "pending"
    assert response.approval_fingerprint == approval_fingerprint(approval)


@pytest.mark.asyncio
async def test_read_approval_hides_missing_or_foreign_approval() -> None:
    creator_id = uuid.uuid4()
    thread = _thread(creator_id)
    db = SimpleNamespace(execute=AsyncMock(side_effect=[_Result(thread), _Result(None)]))

    with pytest.raises(RuntimeFailure) as failure:
        await read_approval(
            db,
            thread_id=thread.id,
            approval_id=uuid.uuid4(),
            creator_id=creator_id,
        )

    assert failure.value.status_code == 404
    assert failure.value.code == "approval_not_found"
    assert failure.value.phase == "approval"
