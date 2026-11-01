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

from contextvars import ContextVar, Token

# KRI-898: where `decide_approval` records the approving app build on the render execution
# (read back by `_claim_approval_dispatch`, stamped on the Job as `creator_client_protocol`).
APPROVAL_CLIENT_PROTOCOL_FIELD = "approval_client_protocol"
CAPTION_STYLE_SCOPE_PROTOCOL = 25
RENDERED_TIMELINE_CLOCK_PROTOCOL = 26
# The app build skipped 26 and went straight to 27: the first build that decodes
# `speaker_background`.
SPEAKER_BACKGROUND_PROTOCOL = 27
# The first build that decodes the `clipTilt` capability (`TimelineClip.backdrop`).
CLIP_TILT_PROTOCOL = 28
# The first build that decodes a clip backdrop of kind `media` (the `clipBackdropMedia`
# capability).
CLIP_BACKDROP_MEDIA_PROTOCOL = 29


def caption_style_scope_supported() -> bool:
    protocol = current_client_protocol()
    return (
        not in_http_request() or protocol is not None and protocol >= CAPTION_STYLE_SCOPE_PROTOCOL
    )


_client_protocol: ContextVar[int | None] = ContextVar("kria_client_protocol", default=None)
_in_http_request: ContextVar[bool] = ContextVar("kria_in_http_request", default=False)


def set_client_protocol(value: int | None) -> None:
    """Record the declared protocol and mark the context as an HTTP request."""
    _client_protocol.set(value)
    _in_http_request.set(True)


def bind_sending_build(value: int | None) -> Token[int | None]:
    """A worker acting on a message reads the protocol of the app build that sent it.

    Only the protocol is bound: ``in_http_request()`` stays False, so request-only gates
    (the 426s on Save) still never fire in a worker, while capability maps that require
    a qualifying build (``editor_capabilities.caption_animation``) see the sender.
    """
    return _client_protocol.set(value)


def unbind_sending_build(token: Token[int | None]) -> None:
    _client_protocol.reset(token)


def clear_request_context() -> None:
    _client_protocol.set(None)
    _in_http_request.set(False)


def current_client_protocol() -> int | None:
    return _client_protocol.get()


def in_http_request() -> bool:
    return _in_http_request.get()
