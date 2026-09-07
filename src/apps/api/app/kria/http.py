"""HTTP boundary that keeps runtime-v2 failures on one typed contract."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Coroutine
from typing import Any

import structlog
from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from starlette.exceptions import HTTPException

from app.kria.api_schemas import KriaProblemOut
from app.kria.contracts import KriaProblem
from app.kria.runtime import RuntimeFailure

log = structlog.get_logger()


def _uses_kria_cursor_contract(request: Request) -> bool:
    return "after_sequence" in request.query_params or "before_sequence" in request.query_params


def problem_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    phase: str = "accept",
    retryable: bool = False,
    recovery: str = "none",
    current_revision: int | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    trace_id = getattr(request.state, "request_id", None) or uuid.uuid4().hex
    problem = KriaProblem(
        code=code,
        phase=phase,
        message=message,
        retryable=retryable,
        recovery=recovery,
        trace_id=str(trace_id),
        current_revision=current_revision,
    )
    return JSONResponse(
        status_code=status_code,
        content=KriaProblemOut(problem=problem).model_dump(mode="json"),
        headers=headers,
    )


class KriaFailureRoute(APIRoute):
    """Normalize explicit runtime failures while leaving legacy HTTP errors alone."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def failure_handler(request: Request) -> Response:
            try:
                return await original(request)
            except RuntimeFailure as exc:
                return problem_response(
                    request,
                    status_code=exc.status_code,
                    code=exc.code,
                    message=exc.message,
                    phase=exc.phase,
                    retryable=exc.retryable,
                    recovery=exc.recovery,
                    current_revision=exc.current_revision,
                )
            except RequestValidationError:
                if not _uses_kria_cursor_contract(request):
                    raise
                return problem_response(
                    request,
                    status_code=422,
                    code="request_invalid",
                    message="Check the request and try again.",
                    recovery="manual",
                )
            except RateLimitExceeded as exc:
                if not _uses_kria_cursor_contract(request):
                    raise
                standard_response = _rate_limit_exceeded_handler(request, exc)
                rate_headers = {
                    key: value
                    for key, value in standard_response.headers.items()
                    if key.lower() not in {"content-length", "content-type"}
                }
                return problem_response(
                    request,
                    status_code=429,
                    code="rate_limited",
                    message="Too many requests. Try again shortly.",
                    retryable=True,
                    recovery="retry",
                    headers=rate_headers,
                )
            except HTTPException as exc:
                if not _uses_kria_cursor_contract(request):
                    raise
                codes = {
                    401: "authentication_required",
                    403: "access_forbidden",
                    404: "kria_runtime_unavailable",
                    409: "request_conflict",
                }
                message = exc.detail if isinstance(exc.detail, str) else "Request failed."
                return problem_response(
                    request,
                    status_code=exc.status_code,
                    code=codes.get(exc.status_code, "request_failed"),
                    message=message,
                    recovery="manual",
                    headers=dict(exc.headers or {}),
                )
            except Exception as exc:
                if not _uses_kria_cursor_contract(request):
                    raise
                log.exception(
                    "kria_cursor_unhandled_exception",
                    path=request.url.path,
                    method=request.method,
                    error_class=type(exc).__name__,
                )
                return problem_response(
                    request,
                    status_code=500,
                    code="internal_error",
                    message="Kria couldn't complete that request. Retry in a moment.",
                    retryable=True,
                    recovery="retry",
                )

        return failure_handler


class KriaRuntimeRoute(KriaFailureRoute):
    """Normalize every framework failure on runtime-v2-only routes."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def runtime_handler(request: Request) -> Response:
            try:
                return await original(request)
            except RequestValidationError:
                return problem_response(
                    request,
                    status_code=422,
                    code="request_invalid",
                    message="Check the request and try again.",
                    recovery="manual",
                )
            except RateLimitExceeded as exc:
                standard_response = _rate_limit_exceeded_handler(request, exc)
                rate_headers = {
                    key: value
                    for key, value in standard_response.headers.items()
                    if key.lower() not in {"content-length", "content-type"}
                }
                return problem_response(
                    request,
                    status_code=429,
                    code="rate_limited",
                    message="Too many requests. Try again shortly.",
                    retryable=True,
                    recovery="retry",
                    headers=rate_headers,
                )
            except HTTPException as exc:
                codes = {
                    401: "authentication_required",
                    403: "access_forbidden",
                    404: "kria_runtime_unavailable",
                    409: "request_conflict",
                }
                message = exc.detail if isinstance(exc.detail, str) else "Request failed."
                return problem_response(
                    request,
                    status_code=exc.status_code,
                    code=codes.get(exc.status_code, "request_failed"),
                    message=message,
                    recovery="manual",
                    headers=dict(exc.headers or {}),
                )
            except Exception as exc:  # noqa: BLE001 - typed public boundary
                log.exception(
                    "kria_runtime_unhandled_exception",
                    path=request.url.path,
                    method=request.method,
                    error_class=type(exc).__name__,
                )
                return problem_response(
                    request,
                    status_code=500,
                    code="internal_error",
                    message="Kria couldn't complete that request. Retry in a moment.",
                    retryable=True,
                    recovery="retry",
                )

        return runtime_handler
