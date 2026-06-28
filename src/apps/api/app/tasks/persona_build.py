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
from app.config import settings
from app.database import sync_session
from app.models import Persona, User
from app.worker import celery_app

log = structlog.get_logger()


def _analysis_summary(tiktok_profile: dict | None) -> str:
    """Extract the pre-rendered TikTok analysis summary from the persona row JSONB.

    Returns "" when the analysis hasn't landed yet (race) or the enrich failed —
    the injector converts "" to an absent block, keeping the prompt byte-identical
    to the no-TikTok baseline.
    """
    if not tiktok_profile:
        return ""
    analysis = tiktok_profile.get("analysis") or {}
    return str(analysis.get("summary_for_prompts") or "")


@celery_app.task(
    name="app.tasks.persona_build.scrape_tiktok_profile",
    bind=True,
    max_retries=1,
    default_retry_delay=5,
    soft_time_limit=30,
    time_limit=40,
)
def scrape_tiktok_profile(self, persona_id: str, handle: str) -> None:  # noqa: ANN001
    """Fetch public TikTok profile and store on the Persona row.

    Best-effort: failure marks nothing in the DB — the NULL tiktok_profile
    tells the frontend and chat agent to proceed as the no-TikTok path.
    """
    from app.services.tiktok_profile import fetch_profile  # noqa: PLC0415

    profile = fetch_profile(handle)
    if profile is None:
        log.info("scrape_tiktok_profile.no_data", persona_id=persona_id, handle=handle)
        return

    with sync_session() as session:
        row = session.get(Persona, uuid.UUID(str(persona_id)))
        if row is None:
            return
        row.tiktok_profile = dict(profile)
        session.commit()
    log.info("scrape_tiktok_profile.done", persona_id=persona_id, handle=handle)

    # Chain enrichment tasks (best-effort, background). The flat profile above is
    # written before these fire so the interviewer can proceed immediately.
    if settings.tiktok_deep_analysis_enabled:
        analyze_tiktok_profile.delay(str(persona_id), handle)
    if settings.tiktok_style_vision_enabled:
        from app.tasks.style_vision_build import analyze_tiktok_style  # noqa: PLC0415
        analyze_tiktok_style.delay(str(persona_id), handle)


@celery_app.task(
    name="app.tasks.persona_build.analyze_tiktok_profile",
    bind=True,
    max_retries=1,
    default_retry_delay=10,
    soft_time_limit=210,
    time_limit=240,
)
def analyze_tiktok_profile(self, persona_id: str, handle: str) -> None:  # noqa: ANN001
    """Enriched TikTok fetch + LLM distillation → persona.tiktok_profile['analysis'].

    Best-effort: any failure (TikTok blocked, timeout, parse error, DB miss) is
    logged and returns silently. The persona is NEVER marked failed by this task.
    The 'analysis' sub-key is simply absent, and the three injection points
    (persona/plan/hook prompts) fall back to byte-identical generation.

    time_limit=240 << worker visibility_timeout (1900s) invariant holds.
    """
    from app.agents._schemas.tiktok_analysis import TikTokAnalyzerInput  # noqa: PLC0415
    from app.agents.tiktok_analyzer import TikTokAnalyzerAgent  # noqa: PLC0415
    from app.services.tiktok_profile import fetch_profile_enriched  # noqa: PLC0415

    if not settings.tiktok_deep_analysis_enabled:
        return

    clean = handle  # already normalized by scrape_tiktok_profile
    profile = fetch_profile_enriched(clean)
    if profile is None:
        log.info("analyze_tiktok_profile.no_data", persona_id=persona_id, handle=clean)
        return

    agent_input = TikTokAnalyzerInput(
        handle=profile["handle"],
        follower_count=profile.get("follower_count"),
        median_views=profile.get("median_views"),
        videos=[dict(v) for v in profile.get("videos", [])],
    )

    try:
        agent = TikTokAnalyzerAgent(default_client())
        output = agent.run(agent_input, ctx=RunContext(job_id=None))
    except Exception as exc:  # noqa: BLE001
        # Best-effort: analysis failure never marks the persona failed.
        log.warning(
            "analyze_tiktok_profile.agent_failed", persona_id=persona_id, error=str(exc)[:300]
        )
        return

    with sync_session() as session:
        row = session.get(Persona, uuid.UUID(str(persona_id)))
        if row is None:
            return
        # Read-merge-write: preserve the flat keys the interviewer reads.
        blob = dict(row.tiktok_profile or {})
        blob["analysis"] = output.analysis.model_dump()
        row.tiktok_profile = blob
        session.commit()
    log.info(
        "analyze_tiktok_profile.done",
        persona_id=persona_id,
        handle=clean,
        has_summary=bool(output.analysis.summary_for_prompts),
    )


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
        # Inject TikTok analysis at call time (not stored on questionnaire row).
        # If the analysis task hasn't landed yet (race), this is "" and the prompt
        # is byte-identical to the no-TikTok baseline — best-effort by design.
        tiktok_summary = _analysis_summary(persona_row.tiktok_profile)
        questionnaire = PersonaQuestionnaire(**(persona_row.questionnaire or {})).model_copy(
            update={"tiktok_analysis": tiktok_summary}
        )

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
        # Preserve onboarding preferences stored before generation ran (e.g.
        # footage_type_bias from the "What you make" step). These are not part of
        # the generated Persona schema but must survive the overwrite.
        prev = dict(row.persona or {})
        new_persona_dict = persona.to_dict()
        if "footage_type_bias" in prev:
            new_persona_dict["footage_type_bias"] = prev["footage_type_bias"]
        row.persona = new_persona_dict
        row.persona_status = "ready"
        row.error_detail = None
        row.prompt_version = PERSONA_PROMPT_VERSION
        user = session.get(User, row.user_id)
        if user is not None and user.onboarding_status == "pending":
            user.onboarding_status = "persona_ready"
        session.commit()
    log.info("persona_build.ready", persona_id=persona_id)
    # Chain style derivation best-effort (Creator Agent M1). Fires only when
    # USER_STYLE_ENABLED is True; task guards against re-deriving edited styles.
    if settings.user_style_enabled:
        from app.tasks.style_build import derive_user_style  # noqa: PLC0415

        derive_user_style.delay(str(persona_id))


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
        tiktok_summary = _analysis_summary(row.tiktok_profile)
        questionnaire = PersonaQuestionnaire(**(row.questionnaire or {})).model_copy(
            update={"preference_summary": summary, "tiktok_analysis": tiktok_summary}
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
        # Preserve onboarding preferences (e.g. footage_type_bias from "What you make")
        # that live in the same JSONB column but are NOT part of the generated Persona
        # schema. Same preservation pattern as generate_persona — both write paths must
        # carry these keys across or retune silently drops the Step-2 multi-select.
        prev = dict(row.persona or {})
        new_persona_dict = persona.to_dict()
        if "footage_type_bias" in prev:
            new_persona_dict["footage_type_bias"] = prev["footage_type_bias"]
        row.persona = new_persona_dict
        row.persona_status = "ready"
        row.error_detail = None
        row.prompt_version = PERSONA_PROMPT_VERSION
        session.commit()
    log.info("persona_retune.ready", persona_id=persona_id, has_summary=bool(summary))
    # Re-derive style when persona updates (M1 propagation). The task's edited
    # guard protects user overrides — only re-derives non-edited styles.
    if settings.user_style_enabled:
        from app.tasks.style_build import derive_user_style  # noqa: PLC0415

        derive_user_style.delay(str(persona_id))
