from collections.abc import AsyncGenerator

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from app.config import settings

# ---------------------------------------------------------------------------
# Async engine (FastAPI / asyncpg)
# ---------------------------------------------------------------------------
_async_url = settings.asyncpg_database_url

engine = create_async_engine(_async_url, pool_pre_ping=True)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session


# ---------------------------------------------------------------------------
# Sync engine (Celery workers / psycopg2)
# ---------------------------------------------------------------------------
# Shared across all task modules.  Key settings:
#   pool_pre_ping  – detect dead connections before reuse (prevents
#                    "server closed the connection unexpectedly")
#   pool_recycle   – force new connections every 5 min so idle ones never
#                    hit PostgreSQL's idle-timeout on Fly.io
#   pool_size / max_overflow – keep the pool small; Celery workers are
#                    low-concurrency (prefetch_multiplier=1)
_sync_db_url = settings.database_url.replace(
    "postgresql+asyncpg://", "postgresql://"
)
sync_engine = create_engine(
    _sync_db_url,
    pool_pre_ping=True,
    pool_recycle=300,
    pool_size=2,
    max_overflow=3,
)


def sync_session() -> Session:
    """Create a sync session for Celery tasks.  Use as a context manager."""
    return Session(sync_engine, expire_on_commit=False)
