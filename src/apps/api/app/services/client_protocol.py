"""The calling native app's declared wire protocol, for the current request.

`X-Kria-Client-Protocol` is the only per-build signal the server gets. Editor
capability maps are computed deep inside helpers that never see the request, so
the request middleware (`app.main.request_correlation`) records the parsed
header here and rollout gates that must keep OLDER app builds closed (KRI-281)
read it back. Outside a request (Celery, scripts, tests) it is ``None``, which
fails such a gate closed.
"""

from __future__ import annotations

from contextvars import ContextVar

_client_protocol: ContextVar[int | None] = ContextVar("kria_client_protocol", default=None)


def set_client_protocol(value: int | None) -> None:
    _client_protocol.set(value)


def current_client_protocol() -> int | None:
    return _client_protocol.get()
