from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import Response

from app.main import app, request_correlation, unhandled_exception_handler


def _request(headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/plan-items/item/assets",
            "raw_path": b"/plan-items/item/assets",
            "query_string": b"",
            "headers": headers or [],
            "client": ("127.0.0.1", 1),
            "server": ("test", 443),
        }
    )


@pytest.mark.asyncio
async def test_fastapi_unhandled_error_is_safe_and_correlated() -> None:
    request = _request(
        [
            (b"x-request-id", b"request-123"),
            (b"x-correlation-id", b"batch-456"),
        ]
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "same_ids", "expected_length"),
    [
        ([], True, 32),
        (
            [(b"x-request-id", b"r" * 200), (b"x-correlation-id", b"c" * 200)],
            True,
            32,
        ),
    ],
)
async def test_request_middleware_generates_or_truncates_ids_on_real_response(
    headers: list[tuple[bytes, bytes]],
    same_ids: bool,
    expected_length: int,
) -> None:
    request = _request(headers)

    async def call_next(_request: Request) -> Response:
        return Response(status_code=204)

    response = await request_correlation(request, call_next)
    request_id = response.headers["x-request-id"]
    correlation_id = response.headers["x-correlation-id"]

    assert len(request_id) == expected_length
    assert len(correlation_id) == expected_length
    assert (request_id == correlation_id) is same_ids
    assert request.state.request_id == request_id
    assert request.state.correlation_id == correlation_id


def test_real_unhandled_response_is_safe_correlated_and_cors_readable() -> None:
    path = "/__tests__/correlated-error"

    async def _raise_private_error() -> None:
        raise RuntimeError("private database detail")

    app.add_api_route(path, _raise_private_error, methods=["GET"])
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get(
                path,
                headers={
                    "Origin": "https://www.usekria.com",
                    "X-Request-Id": "request-123",
                    "X-Correlation-Id": "batch-456",
                },
            )
    finally:
        app.router.routes[:] = [
            route for route in app.router.routes if getattr(route, "path", None) != path
        ]

    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"
    assert response.headers["x-request-id"] == "request-123"
    assert response.headers["x-correlation-id"] == "batch-456"
    assert response.headers["access-control-allow-origin"] == "https://www.usekria.com"
    assert response.headers["access-control-allow-credentials"] == "true"
    assert "private database detail" not in response.text
