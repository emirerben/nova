"""A PostgreSQL deadlock must surface as a retryable 409, never a raw 500.

Production returned 500s on ``POST /creation-threads/{id}/media`` when two
requests took the PlanItem/Job row locks in opposite order.  The lock order is
fixed (and statically guarded in ``tests/routes/test_lock_order.py``), but a
deadlock is inherently transient: any remaining one must reach the client as a
conflict it can retry, not as an opaque server error.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy.exc import DBAPIError, OperationalError
from starlette.requests import Request

from app.main import transient_db_conflict_handler


class _PGError(Exception):
    """Stand-in for the asyncpg error SQLAlchemy wraps, which carries sqlstate."""

    def __init__(self, sqlstate: str) -> None:
        super().__init__("deadlock detected")
        self.sqlstate = sqlstate


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/creation-threads/8926e4b7/media",
            "raw_path": b"/creation-threads/8926e4b7/media",
            "query_string": b"",
            "headers": [(b"x-request-id", b"request-123")],
            "client": ("127.0.0.1", 1),
            "server": ("test", 443),
        }
    )


def _wrapped(sqlstate: str) -> DBAPIError:
    return OperationalError("SELECT 1", {}, _PGError(sqlstate))


@pytest.mark.asyncio
@pytest.mark.parametrize("sqlstate", ["40P01", "40001"])
async def test_transient_conflict_becomes_retryable_409(sqlstate: str) -> None:
    response = await transient_db_conflict_handler(_request(), _wrapped(sqlstate))
    body = json.loads(bytes(response.body))

    assert response.status_code == 409
    assert body["code"] == "concurrent_update"
    assert body["retryable"] is True
    assert response.headers["Retry-After"] == "1"
    # The driver's message may name tables and process ids; it must not leak.
    assert "deadlock detected" not in json.dumps(body)


@pytest.mark.asyncio
async def test_non_transient_db_error_still_500s() -> None:
    """Only serialization failures are retryable; real faults stay 500."""

    response = await transient_db_conflict_handler(_request(), _wrapped("23505"))
    body = json.loads(bytes(response.body))

    assert response.status_code == 500
    assert body["code"] == "internal_error"


@pytest.mark.asyncio
async def test_dbapi_error_without_sqlstate_still_500s() -> None:
    response = await transient_db_conflict_handler(
        _request(), OperationalError("SELECT 1", {}, Exception("boom"))
    )

    assert response.status_code == 500
