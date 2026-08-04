import threading

from celery import Celery
from celery.schedules import crontab
from celery.signals import before_task_publish, task_prerun, task_success, worker_ready

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
        "app.tasks.lyrics_backfill",
        "app.tasks.auto_music_orchestrate",
        "app.tasks.generative_build",
        "app.tasks.persona_build",
        "app.tasks.style_build",
        "app.tasks.style_vision_build",
        "app.tasks.content_plan_build",
        "app.tasks.online_eval",
        "app.tasks.grade_final_video",
        "app.tasks.send_daily_digest",
        "app.tasks.maintenance",
        "app.tasks.conformance_build",
        "app.tasks.transcript_analyze",
        "app.tasks.autoplace",
        "app.tasks.omni_generate",
        "app.tasks.tiktok",
        "app.tasks.account_lifecycle",
    ],
)

# Every Beat-triggered task, routed to the `light` process's `maintenance`
# queue (fly.toml) instead of the `worker` process's celery/plan-jobs/
# overlay-jobs queues. This is the render-worker autostop split: the render
# worker can only sit idle (and be safely stopped) if NOTHING recurring is
# scheduled onto it — before this, the 5-min sweep_stale_jobs entry alone
# guaranteed the worker was never idle for more than a few minutes.
# `cleanup_cancelled_job` isn't Beat-scheduled (it's dispatched on-demand
# from an admin cancel action) but is included here for the same reason:
# it's a lightweight cleanup task, not a render task, and has no business
# waking a stopped render machine.
#
# There is no wildcard/pattern route here on purpose — every entry is an
# explicit, named task. A future Beat entry that isn't added here defaults
# to the `celery` queue (the render worker's default queue), which is the
# WRONG failure direction for a maintenance task (it would land on a
# machine that might be stopped) but is at least loud in effect: Beat
# entries are rare and reviewed, unlike per-request dispatch call sites.
MAINTENANCE_TASK_NAMES: tuple[str, ...] = (
    "tasks.sweep_stale_jobs",
    "tasks.cleanup_agent_runs",
    "tasks.send_daily_digest",
    "tasks.cleanup_cancelled_job",
    "app.tasks.tiktok.poll_tiktok_publications",
    "app.tasks.tiktok.schedule_tiktok_account_syncs",
    "app.tasks.tiktok.cleanup_tiktok_publications",
    # The render-worker lifecycle task itself MUST run on `light`, never on
    # the `worker` machine it's managing — obviously.
    "tasks.manage_render_worker_lifecycle",
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
    # Prefork child recycling (2026-07-21 OOM, job e8173a25): a burst of
    # analyze_pool_asset tasks leaves CLIP/torch/Whisper residency in the single
    # long-lived child (concurrency=1), and the NEXT render's ffmpeg peak then
    # stacks on top of it. Semantics (billiard, Linux): the check is
    # getrusage(RUSAGE_SELF).ru_maxrss — the child's lifetime PEAK RSS, which
    # never decreases — compared after each task completes; the child is
    # replaced BETWEEN tasks, never mid-task, so acks_late semantics are
    # untouched. Peak-not-current means one transient >3GB spike marks the
    # child for recycle even if the memory was freed — deliberate here (we
    # WANT a fresh child after any task that ballooned), but it also means the
    # threshold must sit above routine task peaks or every task recycles.
    # Validate in prod via billiard's "child process exceeding memory limit"
    # log line: occasional = working as intended; every task = raise the
    # threshold. The replacement forks from the parent, which holds the
    # prewarmed CLIP singleton (copy-on-write), so the recycle is cheap.
    # A dedicated queue/machine for analysis tasks was considered and
    # rejected: concurrency=1 already serializes execution — the problem was
    # residual memory, not co-execution. 0 disables.
    **(
        {"worker_max_memory_per_child": settings.worker_max_memory_per_child_kb}
        if settings.worker_max_memory_per_child_kb > 0
        else {}
    ),
    # task_routes: send every maintenance task to the `light` process's
    # queue instead of the render worker's default queue. Everything NOT
    # listed here keeps Celery's normal resolution (the task's own `queue=`
    # kwarg if one was passed at dispatch time, else the default `celery`
    # queue) — this dict is additive, not a full routing table.
    task_routes={name: {"queue": "maintenance"} for name in MAINTENANCE_TASK_NAMES},
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
        "poll-tiktok-publications-every-minute": {
            "task": "app.tasks.tiktok.poll_tiktok_publications",
            "schedule": 60.0,
        },
        "schedule-tiktok-syncs-every-15-min": {
            "task": "app.tasks.tiktok.schedule_tiktok_account_syncs",
            "schedule": 900.0,
        },
        "cleanup-tiktok-snapshots-daily": {
            "task": "app.tasks.tiktok.cleanup_tiktok_publications",
            "schedule": crontab(hour=4, minute=30),
        },
        # Render-worker autostop lifecycle (stop-when-idle + start-backstop).
        # 2 min: short enough that a missed/failed wake-hook call (worker.py's
        # before_task_publish signal) only costs a bounded, small delay before
        # this backstop notices queued work and starts the machine anyway.
        # A complete no-op when RENDER_AUTOSTOP_ENABLED is off — see
        # app/tasks/maintenance.py:manage_render_worker_lifecycle.
        "manage-render-worker-lifecycle-every-2-min": {
            "task": "tasks.manage_render_worker_lifecycle",
            "schedule": 120.0,
        },
    },
)

# Every task name that appears in beat_schedule above, derived directly from
# the dict rather than hand-duplicated into a second list — a future Beat
# entry is automatically covered here with zero extra maintenance. Used by
# the heartbeat signal below to filter "a Beat-scheduled task just
# succeeded" from "some unrelated task (a render, an LLM call) succeeded" —
# only the former is evidence Beat itself is alive and dispatching.
BEAT_SCHEDULED_TASK_NAMES: frozenset[str] = frozenset(
    entry["task"] for entry in celery_app.conf.beat_schedule.values()
)


@task_success.connect
def _record_beat_heartbeat(sender=None, **kwargs):  # pragma: no cover (signal wiring)
    """Record 'a Beat-scheduled task just succeeded' for GET /health/beat.

    See app/services/beat_heartbeat.py for the full rationale: this is the
    ONLY mechanism that can detect Celery Beat being fully dead, since
    anything itself Beat-scheduled shares Beat's exact blind spot. Filtered
    to BEAT_SCHEDULED_TASK_NAMES so a render job succeeding doesn't mask an
    actually-dead Beat process. Fires wherever the task actually executed
    (the `light` process, after the task_routes split above) — proving not
    just that Beat ticked, but that the whole dispatch-to-completion path
    is alive.
    """
    task_name = getattr(sender, "name", None)
    if task_name not in BEAT_SCHEDULED_TASK_NAMES:
        return
    try:
        from app.services.beat_heartbeat import record_beat_task_success  # noqa: PLC0415

        record_beat_task_success()
    except Exception as exc:  # noqa: BLE001
        print(f"[task_success] beat heartbeat record failed (non-fatal): {exc!r}")


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

    from app.tasks.build_task_reaper import reap_stale_build_tasks  # noqa: PLC0415
    from app.tasks.reaper import reap_orphans, reconcile_stuck_variants  # noqa: PLC0415

    def _run():
        try:
            count = reap_orphans(celery_app)
            if count:
                # structlog isn't configured in this signal context;
                # plain print routes to stdout which Fly captures.
                print(f"[worker_ready] reaped {count} orphan job(s) at startup")
        except Exception as exc:  # noqa: BLE001
            print(f"[worker_ready] orphan reap failed (non-fatal): {exc!r}")

        # Variants frozen "rendering" on already-terminal jobs (dead re-renders)
        # are invisible to reap_orphans — sweep them too so the frontend stops
        # polling a tile whose video is actually ready.
        try:
            stuck = reconcile_stuck_variants(celery_app)
            if stuck:
                print(f"[worker_ready] reconciled {stuck} job(s) with stuck variants")
        except Exception as exc:  # noqa: BLE001
            print(f"[worker_ready] stuck-variant reconcile failed (non-fatal): {exc!r}")

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


@task_prerun.connect
def _sweep_stale_tmpdirs(sender=None, **kwargs):  # pragma: no cover (signal wiring)
    """Reclaim a dead attempt's tmpfs footprint before the next task starts.

    A SIGKILL'd prefork child never runs its TemporaryDirectory cleanup; on
    RAM-backed /tmp those orphaned dirs shrink the very memory budget the
    redelivered attempt needs. task_prerun fires in the (possibly respawned)
    child before every task — see app/pipeline/tmp_sweep.py for the age-cutoff
    invariant that keeps this safe for local-dev sibling workers.
    """
    try:
        from app.pipeline.tmp_sweep import sweep_stale_nova_tmpdirs  # noqa: PLC0415

        sweep_stale_nova_tmpdirs()
    except Exception as exc:  # noqa: BLE001 — sweep must never fail a task
        print(f"[task_prerun] tmp sweep failed (non-fatal): {exc!r}")


def _worker_consumes_render_queues(sender) -> bool:
    """True when this worker process consumes at least one render-worker
    queue — the only workers where the CLIP font matcher can ever run.

    `sender` is the celery.worker.consumer.Consumer instance that
    `worker_ready` passes; `sender.app.amqp.queues.consume_from` reflects the
    queues this process was started with. `-Q maintenance` (the `light` Fly
    machine) restricts it to {"maintenance"}; the render worker's
    `-Q celery,plan-jobs,overlay-jobs` and a bare local-dev worker (no `-Q`,
    falls back to every declared queue) both include the default `celery`
    queue.

    Fail-CLOSED (False) when the consumer shape can't be read. The asymmetry:
    wrongly skipping the prewarm on the render worker costs one ~3-5s lazy
    model load on the first template analysis after a deploy; wrongly
    prewarming on the light machine loads torch + ViT-B/32 (~450-600MB peak,
    see clip_font_matcher.py) into a small VM sized for DB/HTTP maintenance
    tasks. That second failure mode is exactly the 2026-08-02 incident: the
    prewarm OOM crash-looped the 512MB light machine until flyd's restart
    budget ran out, parking it `stopped` through every subsequent deploy and
    silently killing the stale-job sweeper, the daily digest, and the TikTok
    polls for two days. See agents/DECISIONS.md (2026-08-04).
    """
    try:
        from app.services.queue_state import RENDER_WORKER_QUEUES  # noqa: PLC0415

        consumed = set(sender.app.amqp.queues.consume_from.keys())
    except Exception as exc:  # noqa: BLE001
        print(f"[worker_ready] queue introspection failed — skipping CLIP prewarm: {exc!r}")
        return False
    if not consumed & RENDER_WORKER_QUEUES:
        print("[worker_ready] no render queue consumed — skipping CLIP prewarm")
        return False
    return True


@worker_ready.connect
def _prewarm_clip_font_matcher(sender, **kwargs):  # pragma: no cover (signal wiring)
    """Load the CLIP font matcher singleton at worker boot.

    Lazy init on first call costs ~3-5s of model-load latency on the
    user-visible critical path. This signal handler pays that cost during
    worker startup so the first template-analysis job after a deploy
    completes promptly. Memory residency is unchanged — the singleton was
    going to load eventually; we just shift the timing.

    Gated to workers that consume a render queue: worker_ready fires in
    EVERY celery worker process, including the small `light` maintenance
    machine where no CLIP-using task can ever execute and the model doesn't
    fit in RAM — see _worker_consumes_render_queues for the incident this
    guards against.

    Runs in a background thread so a slow first-time weights download
    can't block worker readiness.
    """
    if not _worker_consumes_render_queues(sender):
        # The gate already printed the accurate skip reason (no render queue
        # vs introspection failure) — don't restate a guess here.
        return

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


# ── render-worker autostop: wake hook ────────────────────────────────────────
#
# Fly cost-cut plan (agents/DECISIONS.md, ~/.claude/plans/this-month-...):
# the `worker` Fly machine spends most of its life stopped when
# RENDER_AUTOSTOP_ENABLED is on. Something has to wake it the instant a
# render-worker task is dispatched, or every job pays the full
# tasks.manage_render_worker_lifecycle poll interval in latency.
#
# Filters on `routing_key`, not a hand-maintained task-name allowlist. An
# earlier draft of this plan tried curating a list of "render task names" —
# it kept missing tasks (found 5 more just by grepping generative_build.py
# that an initial pass hadn't listed, including tasks that don't render video
# at all but still run on THIS worker, like clip/track analysis). routing_key
# is the actual mechanism Celery uses to decide which physical worker process
# picks up a message — filtering on it structurally closes the whole bypass
# class, including any future task nobody remembers to list here. Verified
# empirically (see the plan doc) that in this app's config (no custom
# exchange topology declared) routing_key always equals the target queue
# name, for both `queue=` kwarg dispatches and the Celery-default queue.

_RENDER_WAKE_DEBOUNCE_KEY = "render_worker:wake_debounce"

_wake_redis_lock = threading.Lock()
_wake_redis_client = None


def _get_wake_redis():  # pragma: no cover (thin wrapper, exercised via the signal test)
    """Module-singleton Redis client for the wake-debounce key.

    Pooled, not per-call — this signal can fire many times per second
    during a burst of dispatches. Mirrors the pattern in
    app/pipeline/clip_cache.py. Returns None on connection failure; callers
    treat that as "can't debounce, fall through and call Fly directly" —
    fail OPEN here (an extra harmless Fly API call), unlike the reaper's
    fail-closed "don't act on unknown" (an extra wake call can't strand a
    job the way a wrongful reap can destroy one).
    """
    global _wake_redis_client
    if _wake_redis_client is not None:
        return _wake_redis_client
    with _wake_redis_lock:
        if _wake_redis_client is not None:
            return _wake_redis_client
        try:
            import redis as redis_lib  # noqa: PLC0415

            client = redis_lib.from_url(
                settings.redis_url, socket_connect_timeout=2, socket_timeout=2
            )
            client.ping()
            _wake_redis_client = client
        except Exception as exc:  # noqa: BLE001
            print(f"[render-worker-wake] redis unavailable (non-fatal): {exc!r}")
            return None
    return _wake_redis_client


def _maybe_start_render_worker() -> None:
    """Debounced call to Fly's start-machine API for the render worker.

    Debounce TTL (RENDER_WAKE_DEBOUNCE_S, default 60s) is intentionally much
    shorter than RENDER_IDLE_GRACE_MIN (default 10min, the lifecycle task's
    idle-before-stop threshold) — within the debounce window, the lifecycle
    task cannot possibly have decided to stop the machine again (it requires
    10 continuous idle minutes first), so a "we already woke it recently"
    skip can never coincide with a legitimate stop-then-restart. This is
    why no live Fly state check is needed here: the debounce alone is
    correct, and the periodic lifecycle backstop is what actually
    guarantees a missed/skipped wake gets recovered — this function is a
    pure latency optimization, never the correctness mechanism.
    """
    from app.services.fly_machines import start_render_worker  # noqa: PLC0415

    redis_client = _get_wake_redis()
    acquired = True
    if redis_client is not None:
        try:
            acquired = bool(
                redis_client.set(
                    _RENDER_WAKE_DEBOUNCE_KEY,
                    "1",
                    nx=True,
                    ex=settings.RENDER_WAKE_DEBOUNCE_S,
                )
            )
        except Exception as exc:  # noqa: BLE001
            # Debounce check itself failed — fail open (call Fly anyway)
            # rather than silently never waking the machine.
            print(f"[render-worker-wake] debounce set failed, calling Fly anyway: {exc!r}")
            acquired = True

    if not acquired:
        return

    start_render_worker()


@before_task_publish.connect
def _wake_render_worker_on_dispatch(
    sender=None, routing_key=None, **kwargs
):  # pragma: no cover (signal wiring; behavior tested via _maybe_start_render_worker)
    """Wake the render worker the instant a render-queue task is dispatched.

    Fires for EVERY task publish across the whole app (`.delay()`,
    `.apply_async()`, and the enqueue_orchestrator helper alike — it's a
    Celery-level signal, not tied to any one dispatch helper), filtered to
    only the queues the `worker` Fly process actually consumes
    (RENDER_WORKER_QUEUES). No-ops entirely when RENDER_AUTOSTOP_ENABLED is
    off — checked FIRST so the flag being off costs nothing beyond one
    settings read, matching the kill-switch convention (byte-identical to
    pre-autostop behavior).

    Runs the actual Fly API call in a background thread with its own
    tight timeout (see app/services/fly_machines.py) — most dispatch sites
    run inside FastAPI async request handlers on the `api` process, and a
    synchronous HTTP call to Fly's API inline here would hang the user's
    request if Fly's API is ever slow. Errors are swallowed: the periodic
    lifecycle backstop recovers a missed wake within its poll interval
    regardless.
    """
    if not settings.RENDER_AUTOSTOP_ENABLED:
        return
    from app.services.queue_state import RENDER_WORKER_QUEUES  # noqa: PLC0415

    if routing_key not in RENDER_WORKER_QUEUES:
        return

    def _run():
        try:
            _maybe_start_render_worker()
        except Exception as exc:  # noqa: BLE001
            print(f"[render-worker-wake] failed (non-fatal): {exc!r}")

    threading.Thread(target=_run, daemon=True, name="render-worker-wake").start()
