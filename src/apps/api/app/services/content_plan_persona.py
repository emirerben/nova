"""Fail-closed ownership checks for a content plan's linked persona.

``content_plans.user_id`` and ``personas.user_id`` are separate tenant keys.
The foreign key on ``content_plans.persona_id`` proves that a persona exists,
but it does not prove that both rows belong to the same user.  Every boundary
that follows a plan's persona link must therefore validate both keys.
"""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models import ContentPlan, Persona

PLAN_PERSONA_OWNERSHIP_CONFLICT_DETAIL = "Content plan is unavailable"
log = structlog.get_logger()


class PlanPersonaOwnershipError(RuntimeError):
    """The plan is quarantined or its persona is not owned by the plan owner."""

    def __init__(self, plan: ContentPlan) -> None:
        self.plan_id = plan.id
        self.plan_user_id = plan.user_id
        self.persona_id = plan.persona_id
        # Keep the exception itself safe to surface in generic logs.  In
        # particular, do not include identifiers in the rendered message.  The
        # typed attributes above are available to trusted diagnostics.
        super().__init__("content plan persona ownership validation failed")


def is_plan_persona_owned(
    plan: ContentPlan,
    persona: Persona | None,
) -> bool:
    """Return whether ``persona`` is the non-quarantined plan owner's row."""

    return (
        getattr(plan, "ownership_quarantined_at", None) is None
        and persona is not None
        and persona.id == plan.persona_id
        and persona.user_id == plan.user_id
    )


def require_plan_persona_owned(plan: ContentPlan, persona: Persona | None) -> Persona:
    """Return the persona or raise the route/task-neutral ownership error."""

    if not is_plan_persona_owned(plan, persona):
        log.warning(
            "content_plan.persona_owner_mismatch",
            plan_id=str(plan.id),
            plan_user_id=str(plan.user_id),
            persona_id=str(plan.persona_id),
        )
        raise PlanPersonaOwnershipError(plan)
    return persona


async def load_owned_plan_persona(
    db: AsyncSession,
    plan: ContentPlan,
    *,
    for_update: bool = False,
) -> Persona:
    """Load the persona only when both plan linkage and tenant ownership match.

    Callers requesting a row lock must already hold the corresponding plan lock;
    the project-wide mutation order is ContentPlan -> Persona -> PlanItem.
    """

    if getattr(plan, "ownership_quarantined_at", None) is not None:
        return require_plan_persona_owned(plan, None)

    stmt = select(Persona).where(
        Persona.id == plan.persona_id,
        Persona.user_id == plan.user_id,
    )
    if for_update:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    persona = (await db.execute(stmt)).scalar_one_or_none()
    return require_plan_persona_owned(plan, persona)


def load_owned_plan_persona_sync(
    session: Session,
    plan: ContentPlan,
    *,
    for_update: bool = False,
) -> Persona:
    """Synchronous twin used by worker tasks at the same ownership boundary."""

    if getattr(plan, "ownership_quarantined_at", None) is not None:
        return require_plan_persona_owned(plan, None)

    stmt = select(Persona).where(
        Persona.id == plan.persona_id,
        Persona.user_id == plan.user_id,
    )
    if for_update:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    persona = session.execute(stmt).scalar_one_or_none()
    return require_plan_persona_owned(plan, persona)
