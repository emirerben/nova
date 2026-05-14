from celery import Celery
from celery.signals import worker_ready

from app.config import settings

# CRITICAL: result_backend MUST be redis:// — default rpc:// silently breaks chords
celery_app = Celery(
    "nova",
    broker=settings.redis_url,
    result_backend=settings.redis_url,
    include=[
        "app.tasks.orchestrate",
        "app.tasks.template_orchestrate",
        "app.tasks.agentic_template_build",
        "app.tasks.drive_import",
        "app.tasks.music_orchestrate",
        "app.tasks.online_eval",
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


@worker_ready.connect
def _reap_orphans_on_startup(sender, **kwargs):  # pragma: no cover (signal wiring)
    """Sweep zombie jobs the previous worker generation left in the DB.

    Why on startup: the orphans we saw in prod (97 jobs over multiple days,
    all with status_template_ready_or_processing AND failure_reason=None)
    were created by SIGKILL'd workers. The next worker generation is the
    natural place to clean up — no Celery beat process needed, no admin
    endpoint, no external scheduler. Just opportunistic sweep on every
    worker boot (which happens at every deploy — exactly when most orphans
    get created).

    Safety: cross-checks Celery `inspect()` so a job that's actively
    running on a sibling worker is NEVER reaped. Threshold is 2× the
    multi-clip hard time_limit (3600s) so legitimately slow finishers
    win the race. Inspection failures (broker hiccup, etc.) are no-ops.

    Runs in a background thread to avoid blocking worker startup.
    """
    import threading  # noqa: PLC0415

    from app.tasks.reaper import reap_orphans  # noqa: PLC0415

    def _run():
        try:
            count = reap_orphans(celery_app)
            if count:
                # structlog isn't configured in this signal context;
                # plain print routes to stdout which Fly captures.
                print(f"[worker_ready] reaped {count} orphan job(s) at startup")
        except Exception as exc:  # noqa: BLE001
            print(f"[worker_ready] orphan reap failed (non-fatal): {exc!r}")

    threading.Thread(target=_run, daemon=True, name="orphan-reaper").start()
