from celery import Celery

from app.config import settings

# CRITICAL: result_backend MUST be redis:// — default rpc:// silently breaks chords
celery_app = Celery(
    "nova",
    broker=settings.redis_url,
    result_backend=settings.redis_url,
    include=[
        "app.tasks.orchestrate",
        "app.tasks.template_orchestrate",
        "app.tasks.drive_import",
        "app.tasks.music_orchestrate",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_acks_late=True,  # requeue on worker death
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,  # don't prefetch — FFmpeg tasks are long
    result_expires=3600,
)
