from celery import Celery
from celery.schedules import crontab
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
        "app.tasks.lyrics_preview_task",
        "app.tasks.auto_music_orchestrate",
        "app.tasks.generative_build",
        "app.tasks.persona_build",
        "app.tasks.style_build",
        "app.tasks.content_plan_build",
        "app.tasks.online_eval",
        "app.tasks.grade_final_video",
        "app.tasks.send_daily_digest",
        "app.tasks.maintenance",
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
    # Beat schedule — picked up only when a `celery beat` process is
    # running (see fly.toml [processes].beat). The worker process ignores
    # this dict, so it's safe to define unconditionally.
    beat_schedule={
        "sweep-stale-jobs-every-5-min": {
            "task": "tasks.sweep_stale_jobs",
            "schedule": 300.0,  # seconds
        },
        # Daily at 04:00 UTC (low-traffic window). Pruned rows = job-scoped
        # agent_run entries older than `agent_run_retention_days` (30d
        # default). Bound by the in-task batch loop, so a first-run
        # backfill never holds locks long. See tasks/maintenance.py.
        "cleanup-agent-runs-daily": {
            "task": "tasks.cleanup_agent_runs",
            "schedule": crontab(hour=4, minute=0),
        },
        # Daily dev-loop heartbeat digest (plan D5 / T7). 13:00 UTC ≈ morning
        # for the founder, within the builder cron's work-hours window so the
        # dead-man's-switch can meaningfully fire on a no-activity weekday.
        # Skips silently if DIGEST_RECIPIENT_EMAIL / RESEND_API_KEY are unset.
        "dev-loop-heartbeat-digest-daily": {
            "task": "tasks.send_daily_digest",
            "schedule": crontab(hour=13, minute=0),
        },
    },
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

    from app.tasks.build_task_reaper import reap_stale_build_tasks  # noqa: PLC0415
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

        # Sweep zombie build_tasks the same way: a builder run (GH Actions) that
        # died mid-task leaves a row stuck `in_progress`. Best-effort, never
        # fatal — modeled on the orphan reap above.
        try:
            summary = reap_stale_build_tasks()
            if summary["total"]:
                print(
                    f"[worker_ready] reaped {summary['total']} stale build_task(s) "
                    f"(requeued={summary['requeued']} blocked={summary['blocked']})"
                )
        except Exception as exc:  # noqa: BLE001
            print(f"[worker_ready] build_task reap failed (non-fatal): {exc!r}")

    threading.Thread(target=_run, daemon=True, name="orphan-reaper").start()


@worker_ready.connect
def _prewarm_clip_font_matcher(sender, **kwargs):  # pragma: no cover (signal wiring)
    """Load the CLIP font matcher singleton at worker boot.

    Lazy init on first call costs ~3-5s of model-load latency on the
    user-visible critical path. This signal handler pays that cost during
    worker startup so the first template-analysis job after a deploy
    completes promptly. Memory residency is unchanged — the singleton was
    going to load eventually; we just shift the timing.

    Runs in a background thread so a slow first-time weights download
    can't block worker readiness.
    """
    import threading  # noqa: PLC0415

    def _run():
        try:
            from app.services.clip_font_matcher import get_matcher  # noqa: PLC0415

            get_matcher()
            print("[worker_ready] CLIP font matcher prewarmed")
        except Exception as exc:  # noqa: BLE001
            # Non-fatal: lazy load retries on first real call, and template
            # analysis wraps font_identification in its own try/except.
            print(f"[worker_ready] CLIP matcher prewarm failed (non-fatal): {exc!r}")

    threading.Thread(target=_run, daemon=True, name="clip-prewarm").start()
