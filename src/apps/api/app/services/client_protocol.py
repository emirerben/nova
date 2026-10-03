"""The calling native app's declared wire protocol, for the current request.

`X-Kria-Client-Protocol` is the only per-build signal the server gets. Editor
capability maps are computed deep inside helpers that never see the request, so
the request middleware (`app.main.request_correlation`) records the parsed
header here and rollout gates that must keep OLDER app builds closed (KRI-281)
read it back.

Outside an HTTP request (Celery workers, sync runtime approvals, scripts) there
is no app build to qualify, so `in_http_request()` is False and such gates
apply their server-side checks only; inside a request a missing or low protocol
fails the gate closed.
"""

from __future__ import annotations

from contextvars import ContextVar

_client_protocol: ContextVar[int | None] = ContextVar("kria_client_protocol", default=None)
_in_http_request: ContextVar[bool] = ContextVar("kria_in_http_request", default=False)


def set_client_protocol(value: int | None) -> None:
    """Record the declared protocol and mark the context as an HTTP request."""
    _client_protocol.set(value)
    _in_http_request.set(True)


def clear_request_context() -> None:
    _client_protocol.set(None)
    _in_http_request.set(False)


def current_client_protocol() -> int | None:
    return _client_protocol.get()


def in_http_request() -> bool:
    return _in_http_request.get()
