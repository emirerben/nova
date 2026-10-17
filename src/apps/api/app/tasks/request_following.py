"""Recurring metadata collection; isolated event-loop connections, no AI calls."""

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.services.request_following_collection import collect_batch
from app.worker import celery_app


async def _collect():
    engine = create_async_engine(settings.asyncpg_database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            return await collect_batch(db)
    finally:
        await engine.dispose()


@celery_app.task(name="app.tasks.request_following.collect", soft_time_limit=100, time_limit=120)
def collect_request_following():
    return asyncio.run(_collect())
