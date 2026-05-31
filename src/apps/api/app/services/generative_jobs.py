"""Single source of truth for constructing a generative-edit Job.

Extracted from `routes/generative_jobs.py` (plan T4) so the public route AND the
content-plan per-item task mint identical jobs with the same clip-path validation.
Persistence + orchestrator dispatch differ between callers (the route is async +
uses `enqueue_orchestrator`; the Celery task is sync + dispatches to the throttled
`plan-jobs` queue), so this builder only constructs + validates the Job. Callers
add/commit and dispatch.
"""

from __future__ import annotations

import uuid

from app.models import Job
from app.routes.admin_music import _validate_clip_path_prefixes

DEFAULT_PLATFORMS = ["tiktok", "instagram", "youtube"]


# Upper bounds on the persona context stashed onto the job. Keeps a runaway
# persona row from bloating all_candidates / the downstream intro_writer prompt.
# (intro_writer re-clamps + re-sanitizes too — this is the storage-side cap.)
_MAX_PERSONA_PILLARS = 8
_MAX_PERSONA_FIELD_CHARS = 400


# Storage-side cap on the feedback summary stashed on the job. The summary is
# already bounded upstream (services/feedback_summary, ≤800), but cap again so a
# stale/oversized value can't bloat all_candidates / the intro_writer prompt.
_MAX_PREFERENCE_SUMMARY_CHARS = 1000


def _build_persona_context(
    *,
    tone: str,
    pillars: list[str] | None,
    theme: str,
    idea: str,
    preference_summary: str = "",
) -> dict | None:
    """Assemble the persona/series context stashed on the job for intro_writer.

    Returns None when every field is empty so public (non-plan) generative jobs
    keep their exact pre-persona `all_candidates` shape — the render path then
    behaves identically to before this change.
    """
    tone = (tone or "").strip()[:_MAX_PERSONA_FIELD_CHARS]
    theme = (theme or "").strip()[:_MAX_PERSONA_FIELD_CHARS]
    idea = (idea or "").strip()[:_MAX_PERSONA_FIELD_CHARS]
    prefs = (preference_summary or "").strip()[:_MAX_PREFERENCE_SUMMARY_CHARS]
    clean_pillars = [
        str(p).strip()[:_MAX_PERSONA_FIELD_CHARS] for p in (pillars or []) if str(p).strip()
    ][:_MAX_PERSONA_PILLARS]
    if not (tone or clean_pillars or theme or idea or prefs):
        return None
    return {
        "tone": tone,
        "content_pillars": clean_pillars,
        "theme": theme,
        "idea": idea,
        "preference_summary": prefs,
    }


def build_generative_job(
    *,
    user_id: uuid.UUID,
    clip_paths: list[str],
    mode: str = "generative",
    language: str = "en",
    selected_platforms: list[str] | None = None,
    content_plan_item_id: uuid.UUID | None = None,
    persona_tone: str = "",
    persona_pillars: list[str] | None = None,
    item_theme: str = "",
    item_idea: str = "",
    preference_summary: str = "",
) -> Job:
    """Construct (not persist) a generative Job after validating clip prefixes.

    Raises ValueError if any clip path is outside the allowlist — callers map
    that to a 422. `mode` is "generative" for the public flow and "content_plan"
    for per-item plan generation; the render path (`orchestrate_generative_job`)
    is identical, `content_plan_item_id` is just the reverse link for admin/debug.

    The `persona_*` / `item_*` args carry the content-plan creator's persona
    (tone + content pillars) and the plan item's theme/idea down to the shared
    `intro_writer` agent so per-item hooks are persona-coherent. They ride
    `all_candidates["persona"]` — the same channel `language` uses — so the
    orchestrator stays decoupled from plan models and the async re-render path
    inherits them. Public jobs pass nothing → the key is omitted entirely.
    """
    if not clip_paths:
        raise ValueError("At least 1 clip is required")
    _validate_clip_path_prefixes(clip_paths)
    all_candidates: dict = {"clip_paths": clip_paths, "language": language}
    persona_ctx = _build_persona_context(
        tone=persona_tone,
        pillars=persona_pillars,
        theme=item_theme,
        idea=item_idea,
        preference_summary=preference_summary,
    )
    if persona_ctx is not None:
        all_candidates["persona"] = persona_ctx
    return Job(
        user_id=user_id,
        job_type="generative",
        mode=mode,
        raw_storage_path=clip_paths[0],
        selected_platforms=selected_platforms or list(DEFAULT_PLATFORMS),
        all_candidates=all_candidates,
        content_plan_item_id=content_plan_item_id,
        status="queued",
    )
