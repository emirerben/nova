from __future__ import annotations

import json

import pytest
from starlette.requests import Request

from app.main import unhandled_exception_handler


@pytest.mark.asyncio
async def test_fastapi_unhandled_error_is_safe_and_correlated() -> None:
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/plan-items/item/assets",
            "raw_path": b"/plan-items/item/assets",
            "query_string": b"",
            "headers": [
                (b"x-request-id", b"request-123"),
                (b"x-correlation-id", b"batch-456"),
            ],
            "client": ("127.0.0.1", 1),
            "server": ("test", 443),
        }
    )

    response = await unhandled_exception_handler(request, RuntimeError("private database detail"))
    body = json.loads(bytes(response.body))

    assert response.status_code == 500
    assert body == {
        "detail": "Kria couldn't complete that request. Retry in a moment.",
        "code": "internal_error",
        "retryable": True,
        "request_id": "request-123",
        "correlation_id": "batch-456",
    }
    assert response.headers["x-request-id"] == "request-123"
    assert response.headers["x-correlation-id"] == "batch-456"
    assert "private database detail" not in str(body)
