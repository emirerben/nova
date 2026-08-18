import re
import uuid

import structlog
from fastapi import FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.config import settings
from app.limiter import limiter
from app.routes import (
    admin,
    admin_build_tasks,
    admin_creator_style,
    admin_generative,
    admin_jobs,
    admin_music,
    admin_plan_items,
    admin_review,
    admin_sound_effects,
    auth,
    clips,
    content_plans,
    generative_jobs,
    landing,
    me,
    music,
    music_jobs,
    personas,
    plan_items,
    presigned,
    sound_effects,
    template_jobs,
    templates,
    tiktok,
    uploads,
    waitlist,
)

log = structlog.get_logger()

app = FastAPI(title="Kria API", version="0.1.0")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

_CORS_ALLOW_ORIGIN_REGEX = r"https://nova-.*-emirerbens-projects\.vercel\.app"
# usekria.com apex 308-redirects to www.usekria.com (Vercel domain config), so the
# origin every real browser sends is the `www` form — it must be allowlisted
# alongside the apex, not instead of it.
_CANONICAL_WEB_ORIGIN = "https://www.usekria.com"
_APEX_WEB_ORIGIN = "https://usekria.com"
_LEGACY_WEB_ORIGIN = "https://nova-video.vercel.app"
_CODE_ALLOWED_ORIGINS = {_CANONICAL_WEB_ORIGIN, _APEX_WEB_ORIGIN, _LEGACY_WEB_ORIGIN}


def _allowed_origins() -> list[str]:
    return sorted(set(settings.allowed_origins) | _CODE_ALLOWED_ORIGINS)


app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins(),
    allow_origin_regex=_CORS_ALLOW_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Job-status polls ship 100KB+ of JSON every 2s per client and nothing between
# uvicorn and the Vercel proxy compresses (the proxy re-encodes toward the
# browser). minimum_size spares tiny health/status bodies.
app.add_middleware(GZipMiddleware, minimum_size=1024)


def _safe_trace_id(value: str | None) -> str | None:
    if value and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value):
        return value
    return None


@app.middleware("http")
async def request_correlation(request: Request, call_next):  # noqa: ANN001
    """Give every HTTP hop a unique request ID and preserve the batch ID."""
    request_id = _safe_trace_id(request.headers.get("x-request-id")) or uuid.uuid4().hex
    correlation_id = _safe_trace_id(request.headers.get("x-correlation-id")) or request_id
    request.state.request_id = request_id
    request.state.correlation_id = correlation_id
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        request_id=request.state.request_id,
        correlation_id=request.state.correlation_id,
    )
    try:
        response = await call_next(request)
    finally:
        structlog.contextvars.clear_contextvars()
    response.headers["X-Request-Id"] = request.state.request_id
    response.headers["X-Correlation-Id"] = request.state.correlation_id
    return response


def _cors_headers_for(request: Request) -> dict[str, str]:
    """Return Access-Control-* headers for the given request's Origin.

    Starlette installs `@app.exception_handler(Exception)` at the
    ServerErrorMiddleware level, which sits OUTSIDE CORSMiddleware in the
    middleware stack (encode/starlette#1175). Responses produced by this
    handler therefore bypass CORSMiddleware entirely on the way out, so we
    have to mirror the allow-origin logic here. Without this, the browser
    sees a 500 with no Access-Control-Allow-Origin and surfaces a
    TypeError: Failed to fetch instead of letting the frontend read the
    status code.
    """
    origin = request.headers.get("origin")
    if not origin:
        return {}
    allowed = origin in _allowed_origins() or bool(re.fullmatch(_CORS_ALLOW_ORIGIN_REGEX, origin))
    if not allowed:
        return {}
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Credentials": "true",
        "Vary": "Origin",
    }


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    request_id = (
        getattr(request.state, "request_id", None)
        or _safe_trace_id(request.headers.get("x-request-id"))
        or uuid.uuid4().hex
    )
    correlation_id = (
        getattr(request.state, "correlation_id", None)
        or _safe_trace_id(request.headers.get("x-correlation-id"))
        or request_id
    )
    log.exception(
        "unhandled_exception",
        path=request.url.path,
        method=request.method,
        request_id=request_id,
        correlation_id=correlation_id,
    )
    return JSONResponse(
        status_code=500,
        content={
            "detail": "Kria couldn't complete that request. Retry in a moment.",
            "code": "internal_error",
            "retryable": True,
            "request_id": request_id,
            "correlation_id": correlation_id,
        },
        headers={
            **_cors_headers_for(request),
            "X-Request-Id": request_id,
            "X-Correlation-Id": correlation_id,
        },
    )


app.include_router(auth.router, prefix="/auth", tags=["auth"])
app.include_router(uploads.router, prefix="/uploads", tags=["uploads"])
app.include_router(admin.router, prefix="/admin", tags=["admin"])
app.include_router(admin_jobs.router, prefix="/admin/jobs", tags=["admin-jobs"])
app.include_router(admin_plan_items.router, prefix="/admin/plan-items", tags=["admin-plan-items"])
app.include_router(admin_music.router, prefix="/admin/music-tracks", tags=["admin-music"])
app.include_router(
    admin_sound_effects.router, prefix="/admin/sound-effects", tags=["admin-sound-effects"]
)
app.include_router(admin_generative.router, prefix="/admin/generative", tags=["admin-generative"])
app.include_router(
    admin_build_tasks.router, prefix="/admin/build-tasks", tags=["admin-build-tasks"]
)
app.include_router(admin_review.router, prefix="/admin/review", tags=["admin-review"])
app.include_router(
    admin_creator_style.router,
    prefix="/admin/creator-style-assignments",
    tags=["admin-creator-style"],
)
app.include_router(template_jobs.router, prefix="/template-jobs", tags=["template-jobs"])
app.include_router(music.router, prefix="/music-tracks", tags=["music"])
app.include_router(sound_effects.router, prefix="/sound-effects", tags=["sound-effects"])
app.include_router(music_jobs.router, prefix="/music-jobs", tags=["music-jobs"])
app.include_router(generative_jobs.router, prefix="/generative-jobs", tags=["generative-jobs"])
app.include_router(personas.router, prefix="/personas", tags=["personas"])
app.include_router(content_plans.router, prefix="/content-plans", tags=["content-plans"])
app.include_router(plan_items.router, prefix="/plan-items", tags=["plan-items"])
app.include_router(me.router, prefix="/me", tags=["me"])
app.include_router(presigned.router, prefix="/presigned-urls", tags=["presigned"])
app.include_router(clips.router, prefix="/clips", tags=["clips"])
app.include_router(templates.router, prefix="/templates", tags=["templates"])
app.include_router(tiktok.router, prefix="/tiktok", tags=["tiktok"])
app.include_router(waitlist.router, tags=["waitlist"])
app.include_router(landing.router, prefix="/landing-clips", tags=["landing"])


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/health/beat")
async def health_beat(response: Response) -> dict:
    """Reports whether Celery Beat is alive, via a heartbeat any
    Beat-scheduled task writes on success (app/services/beat_heartbeat.py).

    Pinged from OUTSIDE this app by .github/workflows/beat-health.yml
    (15-min cron) — the only kind of check immune to Beat's own death,
    since any check that is itself Beat-scheduled shares Beat's exact
    blind spot. Matters
    specifically because RENDER_AUTOSTOP_ENABLED makes Beat's health a
    prerequisite for the render worker ever restarting on a missed
    wake-hook call (see agents/DECISIONS.md).

    Returns 503 (not 200) when unhealthy — most external uptime monitors
    alert on a non-2xx status alone without needing to parse the body, so
    the status code IS the actionable signal, not just the JSON payload.
    """
    from app.services.beat_heartbeat import beat_heartbeat_status  # noqa: PLC0415

    healthy, age_seconds = beat_heartbeat_status()
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": "ok" if healthy else "stale",
        "age_seconds": age_seconds,
    }
