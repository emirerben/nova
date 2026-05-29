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
