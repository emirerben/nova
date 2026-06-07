"""Celery task: derive a per-user text style from persona + TikTok analysis.

Off-Job work (no Job row). Chained from `generate_persona` and
`retune_persona_from_feedback` on success. Best-effort: any failure is logged
and the style column stays NULL → byte-identical render behavior.

Guard: if `personas.style.status == "edited"` the user has hand-edited their
style; derivation never auto-overwrites it (the "user's say wins" invariant).
Use POST /personas/style/rederive to explicitly re-derive an edited style.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import structlog

from app.config import settings
from app.database import sync_session
from app.models import Persona
from app.worker import celery_app

log = structlog.get_logger()


def _analysis_summary(tiktok_profile: dict | None) -> str:
    """Extract TikTok analysis summary from the persona's tiktok_profile JSONB.
    Returns "" when absent — the prompt skips the TikTok block entirely.
    Mirrors persona_build._analysis_summary.
    """
    if not tiktok_profile:
        return ""
    analysis = tiktok_profile.get("analysis") or {}
    return str(analysis.get("summary_for_prompts") or "")


def _build_catalog_inputs():  # noqa: ANN201
    """Load the generative style-set catalog + non-deprecated font vibes."""
    from app.agents.style_derivation import FontEntry, StyleSetEntry  # noqa: PLC0415
    from app.pipeline.style_sets import list_style_sets  # noqa: PLC0415

    available_sets = [StyleSetEntry(**s) for s in list_style_sets(applies_to="generative")]

    # Font registry — import the internal dict, filter deprecated.
    from app.pipeline.text_overlay import _FONT_REGISTRY  # noqa: PLC0415

    font_vibes = [
        FontEntry(
            name=name,
            vibe=str(info.get("vibe", "") or ""),
            category=str(info.get("category", "") or ""),
        )
        for name, info in _FONT_REGISTRY.items()
        if not info.get("deprecated")
    ]
    return available_sets, font_vibes


@celery_app.task(
    name="app.tasks.style_build.derive_user_style",
    bind=True,
    max_retries=1,
    default_retry_delay=15,
    soft_time_limit=120,
    time_limit=150,
)
def derive_user_style(self, persona_id: str, force: bool = False) -> None:  # noqa: ANN001
    """Derive and persist a UserStyle for the given persona.

    Failures are caught and stored (status='failed') so the route can surface
    a "style unavailable" state without crashing. The persona row is NEVER
    marked failed by this task — a style failure is non-fatal.

    `force=True` bypasses the "user's say wins" edited guard. Only used by
    POST /personas/style/rederive, which is an explicit user re-derive request.
    """
    if not settings.user_style_enabled:
        log.debug("style_build.disabled", persona_id=persona_id)
        return

    with sync_session() as session:
        row = session.get(Persona, uuid.UUID(str(persona_id)))
        if row is None:
            log.warning("style_build.missing_row", persona_id=persona_id)
            return
        # "User's say" invariant: never auto-overwrite a hand-edited style.
        # Bypassed when force=True (explicit rederive request via /rederive route).
        existing_style = row.style or {}
        if not force and existing_style.get("status") == "edited":
            log.info("style_build.skip_edited", persona_id=persona_id)
            return
        if not row.persona:
            log.info("style_build.no_persona_yet", persona_id=persona_id)
            return

        persona_dict = dict(row.persona)
        tiktok_summary = _analysis_summary(row.tiktok_profile)

    # Build catalog inputs outside the session (pure CPU, no DB).
    try:
        available_sets, font_vibes = _build_catalog_inputs()
    except Exception as exc:  # noqa: BLE001
        log.warning("style_build.catalog_load_failed", persona_id=persona_id, error=str(exc))
        return

    # Run the agent.
    try:
        from app.agents._model_client import default_client  # noqa: PLC0415
        from app.agents._runtime import RunContext  # noqa: PLC0415
        from app.agents.style_derivation import (  # noqa: PLC0415
            StyleDerivationAgent,
            StyleDerivationInput,
        )

        agent_input = StyleDerivationInput(
            persona_summary=str(persona_dict.get("summary", "") or ""),
            persona_pillars=list(persona_dict.get("content_pillars", []) or []),
            persona_tone=str(persona_dict.get("tone", "") or ""),
            persona_audience=str(persona_dict.get("audience", "") or ""),
            tiktok_analysis_summary=tiktok_summary,
            available_sets=available_sets,
            font_vibes=font_vibes,
        )
        agent = StyleDerivationAgent(default_client())
        output = agent.run(agent_input, ctx=RunContext(job_id=None))
        derived_style = output.style
    except Exception as exc:  # noqa: BLE001
        log.warning("style_build.agent_failed", persona_id=persona_id, error=str(exc)[:400])
        # Persist a failure marker so the UI can show "style failed, retry".
        # Preserve any prior knobs so a transient API failure doesn't erase good data.
        with sync_session() as session:
            row = session.get(Persona, uuid.UUID(str(persona_id)))
            if row is not None and (row.style or {}).get("status") != "edited":
                prior = dict(row.style or {})
                prior["status"] = "failed"
                row.style = prior
                session.commit()
        return

    # Persist the derived style.
    with sync_session() as session:
        row = session.get(Persona, uuid.UUID(str(persona_id)))
        if row is None:
            return
        # Re-check the edited guard (concurrent PATCH may have run).
        # When force=True the caller (rederive route) already set status="deriving"
        # and the user consented to overwrite, so skip this guard.
        if not force and (row.style or {}).get("status") == "edited":
            log.info("style_build.skip_edited_race", persona_id=persona_id)
            return
        row.style = {
            **derived_style.model_dump(),
            "derived_from": {
                "persona_id": str(persona_id),
                "derived_at": datetime.now(UTC).isoformat(),
                "style_version": derived_style.style_version,
            },
        }
        session.commit()
    log.info(
        "style_build.ready",
        persona_id=persona_id,
        style_set_id=derived_style.style_set_id,
        instruction_level=derived_style.instruction_level,
    )
