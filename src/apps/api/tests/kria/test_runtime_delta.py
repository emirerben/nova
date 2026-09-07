from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.kria.runtime import RuntimeFailure, read_delta


class _Result:
    def __init__(self, *, scalar=None, scalars=None):  # noqa: ANN001
        self._scalar = scalar
        self._scalars = list(scalars or [])

    def scalar_one_or_none(self):  # noqa: ANN201
        return self._scalar

    def scalars(self):  # noqa: ANN201
        values = self._scalars

        class _Scalars:
            @staticmethod
            def all():  # noqa: ANN205
                return values

        return _Scalars()


def _thread() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=uuid.uuid4(),
        runtime_version=2,
        status="active",
        revision=12,
    )


def _event(sequence: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        sequence=sequence,
        revision=sequence + 1,
        role="assistant",
        event_type="assistant_response",
        content=f"Update {sequence}",
        payload={"sequence": sequence},
        created_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_omitted_cursor_bootstraps_newest_bounded_page_in_chronological_order() -> None:
    thread = _thread()
    # PostgreSQL returns this descending because bootstrap asks for newest first.
    newest_first = [_event(9), _event(8), _event(7)]
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _Result(scalar=thread),
                _Result(scalars=newest_first),
            ]
        )
    )

    response = await read_delta(
        db,
        thread_id=thread.id,
        creator_id=thread.creator_id,
        after_sequence=-1,
        limit=2,
    )

    event_query = db.execute.await_args_list[1].args[0]
    assert "ORDER BY creation_thread_events.sequence DESC" in str(event_query)
    assert [event.sequence for event in response.events] == [8, 9]
    assert response.after_sequence == -1
    assert response.next_after_sequence == 9
    assert response.previous_before_sequence == 8
    assert response.has_more is True
    assert db.execute.await_count == 2


@pytest.mark.asyncio
async def test_before_cursor_retrieves_every_older_bootstrap_page() -> None:
    thread = _thread()
    older_rows = [_event(7), _event(6), _event(5)]
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _Result(scalar=thread),
                _Result(scalars=older_rows),
            ]
        )
    )

    response = await read_delta(
        db,
        thread_id=thread.id,
        creator_id=thread.creator_id,
        after_sequence=-1,
        before_sequence=8,
        limit=2,
    )

    event_query = str(db.execute.await_args_list[1].args[0])
    assert "creation_thread_events.sequence <" in event_query
    assert "ORDER BY creation_thread_events.sequence DESC" in event_query
    assert [event.sequence for event in response.events] == [6, 7]
    assert response.before_sequence == 8
    assert response.previous_before_sequence == 6
    assert response.has_more is True
    assert db.execute.await_count == 2


@pytest.mark.asyncio
async def test_forward_and_backward_cursors_are_mutually_exclusive() -> None:
    thread = _thread()
    db = SimpleNamespace(execute=AsyncMock(return_value=_Result(scalar=thread)))

    with pytest.raises(RuntimeFailure, match="either after_sequence or before_sequence") as exc:
        await read_delta(
            db,
            thread_id=thread.id,
            creator_id=thread.creator_id,
            after_sequence=4,
            before_sequence=8,
            limit=2,
        )

    assert exc.value.status_code == 422
    assert exc.value.code == "cursor_invalid"
    assert db.execute.await_count == 1


@pytest.mark.asyncio
async def test_explicit_cursor_pages_forward_from_sequence() -> None:
    thread = _thread()
    forward_rows = [_event(6), _event(7), _event(8)]
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _Result(scalar=thread),
                _Result(scalars=forward_rows),
            ]
        )
    )

    response = await read_delta(
        db,
        thread_id=thread.id,
        creator_id=thread.creator_id,
        after_sequence=5,
        limit=2,
    )

    event_query = db.execute.await_args_list[1].args[0]
    rendered_query = str(event_query)
    assert "creation_thread_events.sequence >" in rendered_query
    assert "ORDER BY creation_thread_events.sequence" in rendered_query
    assert "DESC" not in rendered_query
    assert [event.sequence for event in response.events] == [6, 7]
    assert response.after_sequence == 5
    assert response.next_after_sequence == 7
    assert response.previous_before_sequence is None
    assert response.has_more is True
    assert db.execute.await_count == 2


@pytest.mark.asyncio
async def test_delta_query_count_is_constant_when_transcript_length_grows() -> None:
    thread = _thread()
    # The database applies LIMIT before hydration. A project may have 10,000+
    # events, but the read path still performs one ownership query and one
    # bounded event query.
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _Result(scalar=thread),
                _Result(scalars=[_event(9_999), _event(10_000)]),
            ]
        )
    )

    response = await read_delta(
        db,
        thread_id=thread.id,
        creator_id=thread.creator_id,
        after_sequence=9_998,
        limit=50,
    )

    assert [event.sequence for event in response.events] == [9_999, 10_000]
    assert db.execute.await_count == 2
