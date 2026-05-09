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
    # Redis broker visibility_timeout: how long a task can be "in-flight" on a
    # worker before the broker considers it lost and re-delivers to another
    # worker. Default is 1 hour, which is longer than orchestrate_template_job's
    # 1800s hard timeout — and means a SIGKILL'd worker leaves jobs orphaned
    # for up to an hour before recovery. Set to 1900s (just past time_limit)
    # so re-delivery happens promptly without risking duplicate execution
    # while a real task is still running.
    broker_transport_options={"visibility_timeout": 1900},
)
