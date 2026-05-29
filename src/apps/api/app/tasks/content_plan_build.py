"""Celery task: generate a 30-day content plan from a persona.

Off-Job work — enqueued with a plain `.delay()` from the content-plans route.
Loads the `content_plans` row + its `personas` row, runs
`ContentPlanGeneratorAgent`, and writes the resulting `plan_items`. Failure is
non-fatal: the plan row goes to `plan_status='failed'` + `error_detail` and the
user can retry. Partial garbage is never persisted — the agent's `parse()`
clamps/dedupes before this task ever sees items.
"""

from __future__ import annotations

import uuid

import structlog

from app.agents._model_client import default_client
from app.agents._runtime import RunContext
from app.agents._schemas.content_plan import (
    CONTENT_PLAN_PROMPT_VERSION,
    ContentPlanInput,
)
from app.agents._schemas.persona import Persona
from app.agents.content_plan_generator import ContentPlanGeneratorAgent
from app.database import sync_session
from app.models import ContentPlan, PlanItem, User
from app.models import Persona as PersonaRow
from app.worker import celery_app

log = structlog.get_logger()


@celery_app.task(
    name="app.tasks.content_plan_build.generate_content_plan",
    bind=True,
    max_retries=2,
    default_retry_delay=10,
)
def generate_content_plan(self, plan_id: str) -> None:  # noqa: ANN001
    """Generate plan_items for `content_plans.id == plan_id` and persist them."""
    with sync_session() as session:
        plan = session.get(ContentPlan, uuid.UUID(str(plan_id)))
        if plan is None:
            log.warning("content_plan_build.missing_row", plan_id=plan_id)
            return
        persona_row = session.get(PersonaRow, plan.persona_id)
        if persona_row is None or not persona_row.persona:
            _fail(session, plan, "persona is not ready")
            return
        agent_input = ContentPlanInput(
            persona=Persona(**persona_row.persona),
            events=str((plan.events or {}).get("text", "") or ""),
            horizon_days=plan.horizon_days or 30,
        )

    try:
        agent = ContentPlanGeneratorAgent(default_client())
        output = agent.run(agent_input, ctx=RunContext(job_id=None))
    except Exception as exc:  # noqa: BLE001
        log.warning("content_plan_build.failed", plan_id=plan_id, error=str(exc))
        with sync_session() as session:
            plan = session.get(ContentPlan, uuid.UUID(str(plan_id)))
            if plan is not None:
                _fail(session, plan, str(exc))
        raise self.retry(exc=exc) from exc

    with sync_session() as session:
        plan = session.get(ContentPlan, uuid.UUID(str(plan_id)))
        if plan is None:
            return
        # Replace any prior items (re-generation is idempotent per plan).
        for existing in list(plan.items):
            session.delete(existing)
        session.flush()
        for spec in output.items:
            session.add(
                PlanItem(
                    content_plan_id=plan.id,
                    day_index=spec.day_index,
                    theme=spec.theme,
                    idea=spec.idea,
                    filming_suggestion=spec.filming_suggestion or None,
                    item_status="idea",
                )
            )
        plan.plan_status = "ready"
        plan.prompt_version = CONTENT_PLAN_PROMPT_VERSION
        user = session.get(User, plan.user_id)
        if user is not None and user.onboarding_status in ("pending", "persona_ready"):
            user.onboarding_status = "plan_ready"
        session.commit()
    log.info("content_plan_build.ready", plan_id=plan_id, item_count=len(output.items))


def _fail(session, plan: ContentPlan, detail: str) -> None:  # noqa: ANN001
    # content_plans has no error_detail column (Phase 2 schema) — log + mark failed.
    # A failed plan is simply re-generatable from the route.
    log.warning("content_plan_build.mark_failed", plan_id=str(plan.id), detail=detail[:300])
    plan.plan_status = "failed"
    session.commit()


# Throttled queue: per-item generative renders are heavy (3 variants each). The
# worker consumes `plan-jobs` with --concurrency=1 so generate-first-week can't
# fire 7 simultaneous renders and OOM the 6GB worker (plan T3). See fly.toml.
PLAN_JOBS_QUEUE = "plan-jobs"


@celery_app.task(
    name="app.tasks.content_plan_build.generate_plan_item_videos",
    bind=True,
    max_retries=1,
    default_retry_delay=15,
)
def generate_plan_item_videos(self, plan_item_id: str) -> None:  # noqa: ANN001
    """Mint a generative Job for a plan item's themed clips and dispatch its render.

    Reuses the generative pipeline verbatim: build_generative_job (shared with the
    public route) → orchestrate_generative_job UNCHANGED. The only plan-specific
    bits are mode="content_plan", the content_plan_item_id reverse link, and the
    throttled queue. Item render state is derived from this Job's status at read
    time (no PlanItem status write here — plan T2).
    """
    from app.services.generative_jobs import build_generative_job  # noqa: PLC0415
    from app.tasks.generative_build import orchestrate_generative_job  # noqa: PLC0415

    with sync_session() as session:
        item = session.get(PlanItem, uuid.UUID(str(plan_item_id)))
        if item is None:
            log.warning("plan_item_videos.missing_item", plan_item_id=plan_item_id)
            return
        clip_paths = list(item.clip_gcs_paths or [])
        if not clip_paths:
            log.warning("plan_item_videos.no_clips", plan_item_id=plan_item_id)
            return
        plan = session.get(ContentPlan, item.content_plan_id)
        if plan is None:
            return
        try:
            job = build_generative_job(
                user_id=plan.user_id,
                clip_paths=clip_paths,
                mode="content_plan",
                content_plan_item_id=item.id,
            )
        except ValueError as exc:
            log.warning("plan_item_videos.invalid_clips", plan_item_id=plan_item_id, error=str(exc))
            return
        session.add(job)
        session.flush()  # populate job.id
        item.current_job_id = job.id
        job_id = str(job.id)
        session.commit()

    # Dispatch onto the throttled plan-jobs queue (concurrency=1 worker).
    orchestrate_generative_job.apply_async((job_id,), queue=PLAN_JOBS_QUEUE)
    log.info("plan_item_videos.dispatched", plan_item_id=plan_item_id, job_id=job_id)
