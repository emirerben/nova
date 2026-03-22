import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.routes import jobs, uploads

log = structlog.get_logger()

limiter = Limiter(key_func=get_remote_address)

app = FastAPI(title="Nova API", version="0.1.0")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(uploads.router, prefix="/uploads", tags=["uploads"])
app.include_router(jobs.router, prefix="/jobs", tags=["jobs"])


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
