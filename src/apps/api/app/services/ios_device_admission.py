"""Admission fence for the iOS-only creation rollout.

This intentionally only decides request admission.  Render dispatch remains
owned by its existing execution paths so the rollout can be reversed without
changing worker behavior.
"""

import structlog
from fastapi import Request
from fastapi.responses import JSONResponse

from app.auth import parse_kria_client_protocol
from app.config import settings
from app.kria.http import problem_response

log = structlog.get_logger()

_STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_LEGACY_CREATION_PREFIXES = (
    "/creation-threads",
    "/content-plans",
    "/plan-items",
    "/generative-jobs",
    "/music-jobs",
    "/template-jobs",
    "/uploads",
)
_NATIVE_CLOUD_ONLY_PREFIXES = (
    "/content-plans",
    "/music-jobs",
    "/template-jobs",
    "/uploads",
)


def is_state_changing_method(method: str) -> bool:
    """Whether an HTTP method can mutate a creation resource."""

    return method.upper() in _STATE_CHANGING_METHODS


def is_legacy_creation_path(path: str) -> bool:
    """Match a legacy creation router without catching similarly named paths."""

    if any(path == prefix or path.startswith(f"{prefix}/") for prefix in _LEGACY_CREATION_PREFIXES):
        return True
    parts = path.split("/")
    return (
        len(parts) == 5
        and parts[0:3] == ["", "me", "jobs"]
        and bool(parts[3])
        and parts[4] == "open-in-editor"
    )


def is_admin_render_path(path: str) -> bool:
    """Match only admin endpoints which begin a render, not metadata CRUD."""

    parts = path.split("/")
    if parts == ["", "admin", "overlay-preview"]:
        return True
    if len(parts) != 5 or parts[0:3] not in (
        ["", "admin", "templates"],
        ["", "admin", "music-tracks"],
    ):
        return False
    if not parts[3]:
        return False
    if parts[2] == "templates":
        return parts[4] in {"test-job", "rerender-job", "text-preview"}
    return parts[4] in {"lyrics-preview", "test-job", "rerender-job"}


def is_native_cloud_only_path(path: str) -> bool:
    """Paths with no device-rendering contract, even for a current native app."""

    parts = path.split("/")
    if path == "/plan-items/manual-drafts":
        return True
    if (
        len(parts) == 5
        and parts[1] == "plan-items"
        and bool(parts[2])
        and parts[3:] == ["manual-draft", "initialize"]
    ):
        return True
    if path == "/generative-jobs":
        return True
    if len(parts) >= 3 and parts[1] == "generative-jobs":
        # Native uploads remain available, but every mutation under an
        # existing generative Job is a legacy cloud-editor operation. The
        # iPhone editor writes through the plan-item atomic commit and device
        # source contracts instead.
        return parts[2] not in {"upload-url", "uploads"}
    if len(parts) >= 4 and parts[1] == "plan-items" and parts[3] == "slide-post":
        return True
    if len(parts) >= 6 and parts[1] == "plan-items" and parts[3] == "variants":
        # The native editor has exactly two state-changing contracts beneath a
        # variant. Everything else is a cloud reburn/retry surface.
        return parts[5] not in {"editor-commit", "editor-sources"}
    return any(
        path == prefix or path.startswith(f"{prefix}/") for prefix in _NATIVE_CLOUD_ONLY_PREFIXES
    )


def is_native_candidate(authorization: str | None, x_user_id: str | None) -> bool:
    """Identify the native-shaped request before authentication resolves it.

    The Next.js proxy always supplies ``X-User-Id``.  A bearer request without
    it is allowed through to the normal mobile JWT authentication branch after
    meeting the protocol floor, so an invalid JWT remains a normal 401.
    """

    return bool(
        authorization and authorization.startswith("Bearer ") and not (x_user_id or "").strip()
    )


def web_creation_retired_response(request: Request) -> JSONResponse:
    """The stable problem envelope for retired web/cloud creation routes."""

    return problem_response(
        request,
        status_code=410,
        code="web_creation_retired",
        message="Web creation is no longer available. Continue in the Kria app.",
        recovery="manual",
    )


def device_render_unsupported_response(request: Request) -> JSONResponse:
    """Reject a creation path which has no on-device execution contract."""

    return problem_response(
        request,
        status_code=422,
        code="device_render_unsupported",
        message="This kind of project cannot render entirely on this iPhone.",
        recovery="manual",
    )


def creation_mutation_admission(
    request: Request,
    *,
    native_client: bool,
    client_protocol: int | None,
) -> JSONResponse | None:
    """Return a typed rejection while iOS-only creation is enabled, if needed."""

    if not settings.ios_device_only_mode:
        return None
    if not native_client:
        return web_creation_retired_response(request)
    if client_protocol is None or client_protocol < settings.kria_minimum_client_protocol:
        return problem_response(
            request,
            status_code=426,
            code="native_update_required",
            message="Update Kria to continue creating projects.",
            recovery="manual",
        )
    return None


def http_creation_mutation_admission(request: Request) -> JSONResponse | None:
    """Admission decision for the cross-router HTTP fence.

    This deliberately reads headers and path only: route dependencies retain
    authentication, request-body parsing, and database ownership.
    """

    if not (settings.ios_device_only_mode or settings.ios_native_device_only_enabled):
        return None
    if not is_state_changing_method(request.method):
        return None

    path = request.url.path
    if settings.ios_device_only_mode and is_admin_render_path(path):
        log.info(
            "ios_device_only_admission",
            decision="rejected",
            code="web_creation_retired",
            method=request.method,
            path=path,
            client_kind="admin",
        )
        return web_creation_retired_response(request)
    if not is_legacy_creation_path(path):
        return None

    native_client = is_native_candidate(
        request.headers.get("authorization"), request.headers.get("x-user-id")
    )
    if not settings.ios_device_only_mode and not native_client:
        # The new rollout changes native creation only; web remains hybrid.
        return None

    client_protocol = parse_kria_client_protocol(request.headers.get("x-kria-client-protocol"))
    if settings.ios_device_only_mode:
        rejected = creation_mutation_admission(
            request, native_client=native_client, client_protocol=client_protocol
        )
    elif client_protocol is None or client_protocol < settings.kria_minimum_client_protocol:
        rejected = problem_response(
            request,
            status_code=426,
            code="native_update_required",
            message="Update Kria to continue creating projects.",
            recovery="manual",
        )
    else:
        rejected = None
    if rejected is not None:
        code = "native_update_required" if rejected.status_code == 426 else "web_creation_retired"
        log.info(
            "ios_device_only_admission",
            decision="rejected",
            code=code,
            method=request.method,
            path=path,
            client_kind=("native" if code == "native_update_required" else "web"),
            client_protocol=parse_kria_client_protocol(
                request.headers.get("x-kria-client-protocol")
            ),
        )
        return rejected
    if is_native_cloud_only_path(path):
        log.info(
            "ios_device_only_admission",
            decision="rejected",
            code="device_render_unsupported",
            method=request.method,
            path=path,
            client_kind="native",
            client_protocol=parse_kria_client_protocol(
                request.headers.get("x-kria-client-protocol")
            ),
        )
        return device_render_unsupported_response(request)
    log.info(
        "ios_device_only_admission",
        decision="admitted",
        code="device_only_native_allowed",
        method=request.method,
        path=path,
        client_kind="native",
        client_protocol=parse_kria_client_protocol(request.headers.get("x-kria-client-protocol")),
    )
    return None
