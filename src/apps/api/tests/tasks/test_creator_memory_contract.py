from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.tasks.creator_memory import _apply_deterministic_direction, _claim_rows, _retry_delay


def test_retry_delay_is_deterministic_exponential_and_bounded() -> None:
    outbox_id = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    delays = [
        _retry_delay(outbox_id=outbox_id, attempts=attempt).total_seconds()
        for attempt in range(1, 9)
    ]

    assert delays == [
        _retry_delay(outbox_id=outbox_id, attempts=attempt).total_seconds()
        for attempt in range(1, 9)
    ]
    assert delays == sorted(delays)
    assert delays[0] >= 30
    assert delays[-1] <= 15 * 60


def test_claim_rows_leases_only_the_bounded_selected_batch() -> None:
    rows = [
        SimpleNamespace(
            id=uuid.uuid4(),
            status="pending",
            attempts=index,
            lease_until=None,
        )
        for index in range(2)
    ]
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    session = MagicMock()
    session.execute.return_value = result

    claimed = _claim_rows(session, limit=2)

    assert claimed == [str(row.id) for row in rows]
    assert [row.status for row in rows] == ["leased", "leased"]
    assert [row.attempts for row in rows] == [1, 2]
    assert all(row.lease_until is not None for row in rows)
    session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_apply_uses_current_revision_as_compare_and_set_fence() -> None:
    user_id = uuid.uuid4()
    db = AsyncMock()
    db.get.return_value = SimpleNamespace(
        id=user_id,
        creator_memory_enabled=True,
        creator_memory_revision=12,
    )

    class _SessionContext:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *_args):
            return None

    create_item = AsyncMock(return_value=SimpleNamespace(operation_kind="create_item"))
    with (
        patch("app.tasks.creator_memory.AsyncSessionLocal", return_value=_SessionContext()),
        patch(
            "app.services.creator_direction.CreatorDirectionService.create_item",
            create_item,
        ),
    ):
        result = await _apply_deterministic_direction(
            user_id=user_id,
            source_event_id=uuid.uuid4(),
            source_thread_id=uuid.uuid4(),
            message="Always use Playfair Display font",
            candidate="explicit",
            idempotency_key="outbox:test",
        )

    assert result == "create_item"
    assert create_item.await_args.kwargs["expected_revision"] == 12
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_apply_is_safe_noop_when_owner_was_deleted() -> None:
    db = AsyncMock()
    db.get.return_value = None

    class _SessionContext:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *_args):
            return None

    with patch("app.tasks.creator_memory.AsyncSessionLocal", return_value=_SessionContext()):
        result = await _apply_deterministic_direction(
            user_id=uuid.uuid4(),
            source_event_id=None,
            source_thread_id=None,
            message="Never use shadows",
            candidate="explicit",
            idempotency_key="outbox:deleted",
        )

    assert result == "deleted"


@pytest.mark.asyncio
async def test_unbundled_extracted_font_is_saved_as_prompt_only_direction() -> None:
    user_id = uuid.uuid4()
    db = AsyncMock()
    db.get.return_value = SimpleNamespace(
        id=user_id,
        creator_memory_enabled=True,
        creator_memory_revision=3,
    )

    class _SessionContext:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *_args):
            return None

    create_item = AsyncMock(
        return_value=SimpleNamespace(operation_kind="create_item", actor_kind="system")
    )
    with patch(
        "app.services.creator_direction.CreatorDirectionService.create_item",
        create_item,
    ):
        result = await _apply_deterministic_direction(
            user_id=user_id,
            source_event_id=None,
            source_thread_id=None,
            message="Always use Comic Sans font",
            candidate="explicit",
            idempotency_key="outbox:unsupported-font",
            session_factory=lambda: _SessionContext(),
            extraction={
                "operation": "activate_explicit",
                "instruction": "Always use Comic Sans font",
                "category": "video_style",
                "enforcement": "constraint",
                "normalized_key": "font_family",
                "structured_value": {"font_family": "Comic Sans"},
            },
        )

    assert result == "create_item"
    assert create_item.await_args.kwargs["normalized_key"] is None
    assert create_item.await_args.kwargs["structured_value"] is None
