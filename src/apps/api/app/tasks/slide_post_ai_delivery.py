"""Durable original AI request delivery; claimed work is never retried."""

import asyncio
import uuid

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.worker import celery_app


async def _run(request_id=None):
    from app.services.slide_post_ai_delivery import deliver, publish_due

    engine = create_async_engine(settings.asyncpg_database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            if request_id is None:
                return await publish_due(db)
            await deliver(db, uuid.UUID(request_id))
    finally:
        await engine.dispose()


@celery_app.task(name="tasks.execute_slide_post_ai_request", soft_time_limit=240, time_limit=270)
def execute_slide_post_ai_request(request_id):
    return asyncio.run(_run(request_id))


@celery_app.task(name="tasks.reconcile_slide_post_ai_requests", soft_time_limit=45, time_limit=55)
def reconcile_slide_post_ai_requests():
    return asyncio.run(_run())
