import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.config import settings
from app.limiter import limiter
from app.routes import (
    admin,
    admin_music,
    jobs,
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_origin_regex=r"https://nova-.*-emirerbens-projects\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(uploads.router, prefix="/uploads", tags=["uploads"])
app.include_router(jobs.router, prefix="/jobs", tags=["jobs"])
app.include_router(admin.router, prefix="/admin", tags=["admin"])
app.include_router(admin_music.router, prefix="/admin/music-tracks", tags=["admin-music"])
app.include_router(template_jobs.router, prefix="/template-jobs", tags=["template-jobs"])
app.include_router(music.router, prefix="/music-tracks", tags=["music"])
app.include_router(music_jobs.router, prefix="/music-jobs", tags=["music-jobs"])
app.include_router(presigned.router, prefix="/presigned-urls", tags=["presigned"])
app.include_router(templates.router, prefix="/templates", tags=["templates"])
app.include_router(waitlist.router, tags=["waitlist"])


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
