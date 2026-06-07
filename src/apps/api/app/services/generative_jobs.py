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

from app.agents._schemas.edit_format import DEFAULT_EDIT_FORMAT, coerce_edit_format
from app.models import Job
from app.routes.admin_music import _validate_clip_path_prefixes, _validate_voiceover_path

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

# Storage-side cap on the TikTok analysis summary. Mirrors the agent-side
# _MAX_SUMMARY_CHARS (1200); re-capped here so a stale row from before the cap
# existed can't bloat all_candidates / the intro_writer prompt.
_MAX_TIKTOK_SUMMARY_CHARS = 1200


def _build_persona_context(
    *,
    tone: str,
    pillars: list[str] | None,
    theme: str,
    idea: str,
    preference_summary: str = "",
    tiktok_summary: str = "",
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
    tiktok = (tiktok_summary or "").strip()[:_MAX_TIKTOK_SUMMARY_CHARS]
    clean_pillars = [
        str(p).strip()[:_MAX_PERSONA_FIELD_CHARS] for p in (pillars or []) if str(p).strip()
    ][:_MAX_PERSONA_PILLARS]
    if not (tone or clean_pillars or theme or idea or prefs or tiktok):
        return None
    ctx: dict = {
        "tone": tone,
        "content_pillars": clean_pillars,
        "theme": theme,
        "idea": idea,
        "preference_summary": prefs,
    }
    # Only stash tiktok_summary when non-empty so public jobs that never provide
    # it keep an identical all_candidates shape — guards the byte-identity invariant.
    if tiktok:
        ctx["tiktok_summary"] = tiktok
    return ctx


def _build_user_style_context(style: dict | None) -> dict | None:
    """Validate + normalize the raw style JSONB blob for stashing on all_candidates.

    Returns None when the style is absent, empty, or invalid — callers that
    receive None omit the `user_style` key entirely, preserving byte-identical
    all_candidates shape vs pre-M1 (the byte-identity invariant).
    """
    if not style:
        return None
    try:
        from app.agents._schemas.user_style import coerce_user_style  # noqa: PLC0415

        parsed = coerce_user_style(style)
        if parsed is None:
            return None
        return parsed.model_dump()
    except Exception:  # noqa: BLE001 — defensive; bad blob → None → baseline
        return None


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
    edit_format: str = DEFAULT_EDIT_FORMAT,
    voiceover_gcs_path: str | None = None,
    tiktok_summary: str = "",
    user_style: dict | None = None,
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
    # Declared edit shape (montage default). The orchestrator's archetype dispatch
    # resolves it against the footage and falls back to montage when unsupported.
    # Coerce here so an unknown/legacy value can never reach the render path.
    all_candidates: dict = {
        "clip_paths": clip_paths,
        "language": language,
        "edit_format": coerce_edit_format(edit_format),
    }
    # Optional voiceover bed (audio-only). Validated against its own prefix so it can
    # never be mistaken for a footage clip. Omitted entirely when absent → public/song
    # jobs keep their exact pre-voiceover all_candidates shape.
    if voiceover_gcs_path:
        all_candidates["voiceover_gcs_path"] = _validate_voiceover_path(voiceover_gcs_path)
    persona_ctx = _build_persona_context(
        tone=persona_tone,
        pillars=persona_pillars,
        theme=item_theme,
        idea=item_idea,
        preference_summary=preference_summary,
        tiktok_summary=tiktok_summary,
    )
    if persona_ctx is not None:
        all_candidates["persona"] = persona_ctx
    # Per-user style (Creator Agent M1). Omit the key entirely when absent or
    # disabled so public/legacy jobs keep their exact pre-style all_candidates shape
    # (byte-identity invariant — guards the render path's no-style baseline).
    from app.config import settings as _settings  # noqa: PLC0415

    if _settings.user_style_enabled:
        style_ctx = _build_user_style_context(user_style)
        if style_ctx is not None:
            all_candidates["user_style"] = style_ctx
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
