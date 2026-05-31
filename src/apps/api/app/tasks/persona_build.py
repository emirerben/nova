"""Celery task: generate a creator persona from an onboarding questionnaire.

Off-Job work (no Job row, no orchestrator dispatch) — enqueued with a plain
`.delay()` from the personas route. Loads the `personas` row, runs
`PersonaGeneratorAgent` on the stored questionnaire, and writes the editable
persona JSON back. Failure is non-fatal to onboarding: the row goes to
`persona_status='failed'` with an `error_detail`, and the user can hand-edit
the persona (PATCH) to unblock — onboarding never hard-blocks on agent failure.
"""

from __future__ import annotations

import uuid

import structlog

from app.agents._model_client import default_client
from app.agents._runtime import RunContext
from app.agents._schemas.persona import PERSONA_PROMPT_VERSION, PersonaQuestionnaire
from app.agents.persona_generator import PersonaGeneratorAgent
from app.database import sync_session
from app.models import Persona, User
from app.worker import celery_app

log = structlog.get_logger()


@celery_app.task(
    name="app.tasks.persona_build.generate_persona",
    bind=True,
    max_retries=2,
    default_retry_delay=10,
)
def generate_persona(self, persona_id: str) -> None:  # noqa: ANN001
    """Generate persona JSON for `personas.id == persona_id` and persist it."""
    with sync_session() as session:
        persona_row = session.get(Persona, uuid.UUID(str(persona_id)))
        if persona_row is None:
            log.warning("persona_build.missing_row", persona_id=persona_id)
            return
        questionnaire = PersonaQuestionnaire(**(persona_row.questionnaire or {}))

    try:
        agent = PersonaGeneratorAgent(default_client())
        persona = agent.run(questionnaire, ctx=RunContext(job_id=None))
    except Exception as exc:  # noqa: BLE001 — persist failure, then optionally retry
        log.warning("persona_build.failed", persona_id=persona_id, error=str(exc))
        with sync_session() as session:
            row = session.get(Persona, uuid.UUID(str(persona_id)))
            if row is not None:
                row.persona_status = "failed"
                row.error_detail = str(exc)[:1000]
                session.commit()
        # Retry transient model errors; give up quietly after max_retries
        # (the row already reflects 'failed' and the user can hand-edit).
        raise self.retry(exc=exc) from exc

    with sync_session() as session:
        row = session.get(Persona, uuid.UUID(str(persona_id)))
        if row is None:
            return
        row.persona = persona.to_dict()
        row.persona_status = "ready"
        row.error_detail = None
        row.prompt_version = PERSONA_PROMPT_VERSION
        user = session.get(User, row.user_id)
        if user is not None and user.onboarding_status == "pending":
            user.onboarding_status = "persona_ready"
        session.commit()
    log.info("persona_build.ready", persona_id=persona_id)


@celery_app.task(
    name="app.tasks.persona_build.retune_persona_from_feedback",
    bind=True,
    max_retries=2,
    default_retry_delay=10,
)
def retune_persona_from_feedback(self, persona_id: str) -> None:  # noqa: ANN001
    """Re-run persona generation with the user's feedback as context (Phase 2).

    User-triggered "update persona from feedback". The route guards that a
    hand-edited persona (status 'edited') is authoritative and 409s before we get
    here, so this only ever re-tunes an AI-authored persona. Failure is non-fatal:
    the existing persona JSON is untouched (only written on success) and status
    reverts to 'ready', so a flaky retune never nukes a working persona.
    """
    from app.services.feedback_summary import rollup_user_feedback  # noqa: PLC0415

    with sync_session() as session:
        row = session.get(Persona, uuid.UUID(str(persona_id)))
        if row is None:
            log.warning("persona_retune.missing_row", persona_id=persona_id)
            return
        summary = rollup_user_feedback(session, row.user_id)
        questionnaire = PersonaQuestionnaire(**(row.questionnaire or {})).model_copy(
            update={"preference_summary": summary}
        )

    try:
        agent = PersonaGeneratorAgent(default_client())
        persona = agent.run(questionnaire, ctx=RunContext(job_id=None))
    except Exception as exc:  # noqa: BLE001
        log.warning("persona_retune.failed", persona_id=persona_id, error=str(exc))
        with sync_session() as session:
            row = session.get(Persona, uuid.UUID(str(persona_id)))
            if row is not None:
                # Keep the existing good persona; just clear the 'generating' state.
                row.persona_status = "ready"
                session.commit()
        raise self.retry(exc=exc) from exc

    with sync_session() as session:
        row = session.get(Persona, uuid.UUID(str(persona_id)))
        if row is None:
            return
        row.persona = persona.to_dict()
        row.persona_status = "ready"
        row.error_detail = None
        row.prompt_version = PERSONA_PROMPT_VERSION
        session.commit()
    log.info("persona_retune.ready", persona_id=persona_id, has_summary=bool(summary))
