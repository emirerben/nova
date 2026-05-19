import re

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.config import settings
from app.limiter import limiter
from app.routes import (
    admin,
    admin_jobs,
    admin_music,
    clips,
    music,
    music_jobs,
    presigned,
    template_jobs,
    templates,
    uploads,
    waitlist,
)

log = structlog.get_logger()

app = FastAPI(title="Nova API", version="0.1.0")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

_CORS_ALLOW_ORIGIN_REGEX = r"https://nova-.*-emirerbens-projects\.vercel\.app"

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_origin_regex=_CORS_ALLOW_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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
    allowed = origin in settings.allowed_origins or bool(
        re.fullmatch(_CORS_ALLOW_ORIGIN_REGEX, origin)
    )
    if not allowed:
        return {}
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Credentials": "true",
        "Vary": "Origin",
    }


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled_exception", path=request.url.path, method=request.method)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
        headers=_cors_headers_for(request),
    )


app.include_router(uploads.router, prefix="/uploads", tags=["uploads"])
app.include_router(admin.router, prefix="/admin", tags=["admin"])
app.include_router(admin_jobs.router, prefix="/admin/jobs", tags=["admin-jobs"])
app.include_router(admin_music.router, prefix="/admin/music-tracks", tags=["admin-music"])
app.include_router(template_jobs.router, prefix="/template-jobs", tags=["template-jobs"])
app.include_router(music.router, prefix="/music-tracks", tags=["music"])
app.include_router(music_jobs.router, prefix="/music-jobs", tags=["music-jobs"])
app.include_router(presigned.router, prefix="/presigned-urls", tags=["presigned"])
app.include_router(clips.router, prefix="/clips", tags=["clips"])
app.include_router(templates.router, prefix="/templates", tags=["templates"])
app.include_router(waitlist.router, tags=["waitlist"])


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
