"""Single source of truth for constructing a generative-edit Job.

Extracted from `routes/generative_jobs.py` (plan T4) so the public route AND the
content-plan per-item task mint identical jobs with the same clip-path validation.
Persistence + orchestrator dispatch differ between callers (the route is async +
uses `enqueue_orchestrator`; the Celery task is sync + dispatches to the throttled
`plan-jobs` queue), so this builder only constructs + validates the Job. Callers
add/commit and dispatch.
"""

from __future__ import annotations

import re
import uuid

from app.agents._schemas.edit_format import DEFAULT_EDIT_FORMAT, coerce_edit_format
from app.models import Job
from app.routes.admin_music import _validate_clip_path_prefixes, _validate_voiceover_path
from app.schemas.montage_preset import (
    DEFAULT_MONTAGE_PRESET,
    coerce_montage_preset,
)

DEFAULT_PLATFORMS = ["tiktok", "instagram", "youtube"]
CONTENT_PLAN_PRIMARY_VARIANT_POLICY = "content_plan_primary"
_SMART_PRESET_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


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

# Filming-guide caps — mirror the schema-side constants so the storage layer
# independently enforces them without importing the schema.
_MAX_FILMING_GUIDE_SHOTS = 4
_MAX_FILMING_GUIDE_FIELD_CHARS = 300
_MAX_FILMING_GUIDE_DURATION_S = 60
_MIN_FILMING_GUIDE_DURATION_S = 1


def _build_filming_guide_context(filming_guide: list[dict] | None) -> list[dict]:
    """Sanitize + cap the filming_guide from a plan item for stashing on all_candidates.

    Returns an empty list when filming_guide is absent/empty/entirely malformed —
    callers then omit the key entirely (byte-identical to pre-B2 shape, same
    discipline as _build_user_style_context returning None).

    Per-shot guards:
    - Non-dict entries dropped.
    - Non-str ``what`` (null, nested object) → skip the shot.
    - Empty or whitespace-only ``what`` after strip → skip.
    - ``what`` / ``how`` string-capped and stripped.
    - Non-str ``how`` (null) → treated as "".
    - ``duration_s`` coerced to int, clamped [1, 60]; on type/value error → 3.
    - Total capped at _MAX_FILMING_GUIDE_SHOTS.
    """
    if not filming_guide:
        return []
    sanitized: list[dict] = []
    for entry in filming_guide:
        if not isinstance(entry, dict):
            continue
        what_raw = entry.get("what", "")
        if not isinstance(what_raw, str):
            continue
        what = what_raw.strip()[:_MAX_FILMING_GUIDE_FIELD_CHARS]
        if not what:
            continue
        how_raw = entry.get("how", "")
        how = how_raw.strip()[:_MAX_FILMING_GUIDE_FIELD_CHARS] if isinstance(how_raw, str) else ""
        try:
            dur = int(float(entry.get("duration_s", 3)))
        except (TypeError, ValueError, OverflowError):
            dur = 3
        dur = max(_MIN_FILMING_GUIDE_DURATION_S, min(_MAX_FILMING_GUIDE_DURATION_S, dur))
        sanitized.append({"what": what, "how": how, "duration_s": dur})
        if len(sanitized) >= _MAX_FILMING_GUIDE_SHOTS:
            break
    return sanitized


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


def _build_smart_captions_context(raw: dict | None) -> dict[str, str] | None:
    """Validate the server-pinned Smart preset before it enters Job JSONB."""

    if not isinstance(raw, dict):
        return None
    preset_id = str(raw.get("preset_id") or "").strip()
    preset_version = str(raw.get("preset_version") or "").strip()
    if not (
        _SMART_PRESET_TOKEN_RE.fullmatch(preset_id)
        and _SMART_PRESET_TOKEN_RE.fullmatch(preset_version)
    ):
        return None
    sound_design = str(raw.get("sound_design") or "auto")
    if sound_design not in {"auto", "off"}:
        sound_design = "auto"
    context = {
        "preset_id": preset_id,
        "preset_version": preset_version,
        "sound_design": sound_design,
    }
    shadow_id = str(raw.get("shadow_preset_id") or "").strip()
    shadow_version = str(raw.get("shadow_preset_version") or "").strip()
    if _SMART_PRESET_TOKEN_RE.fullmatch(shadow_id) and _SMART_PRESET_TOKEN_RE.fullmatch(
        shadow_version
    ):
        context["shadow_preset_id"] = shadow_id
        context["shadow_preset_version"] = shadow_version
    return context


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
    voiceover_bed_level: float | None = None,
    voiceover_caption_style: str | None = None,
    tiktok_summary: str = "",
    user_style: dict | None = None,
    filming_guide: list[dict] | None = None,
    narrative_shot_count: int = 0,
    clip_notes: dict[str, str] | None = None,
    landscape_fit: str = "fill",
    montage_preset: str = DEFAULT_MONTAGE_PRESET,
    variant_policy: str | None = None,
    smart_captions: dict | None = None,
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
    if variant_policy == CONTENT_PLAN_PRIMARY_VARIANT_POLICY:
        all_candidates["variant_policy"] = CONTENT_PLAN_PRIMARY_VARIANT_POLICY
    smart_context = _build_smart_captions_context(smart_captions)
    if smart_context is not None and all_candidates["edit_format"] == "subtitled":
        all_candidates["smart_captions"] = smart_context
    # Optional voiceover bed (audio-only). Validated against its own prefix so it can
    # never be mistaken for a footage clip. Omitted entirely when absent → public/song
    # jobs keep their exact pre-voiceover all_candidates shape.
    if voiceover_gcs_path:
        all_candidates["voiceover_gcs_path"] = _validate_voiceover_path(voiceover_gcs_path)
    # Narrated original-audio bed level (0..1). Clamped here; omitted when absent so
    # non-narrated / pre-feature jobs keep their exact all_candidates shape.
    if voiceover_bed_level is not None:
        all_candidates["voiceover_bed_level"] = max(0.0, min(1.0, float(voiceover_bed_level)))
    # Narrated caption style ("word" → word-by-word; anything else ignored so the
    # render defaults to sentence captions). Omitted when absent so non-narrated /
    # pre-feature jobs keep their exact all_candidates shape.
    if voiceover_caption_style == "word":
        all_candidates["voiceover_caption_style"] = "word"
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
    # Filming guide (Creator Agent M3 / B2). Plain plan data — NOT gated on
    # USER_STYLE_ENABLED. Omit the key entirely when absent/empty so public
    # and non-plan jobs keep byte-identical all_candidates shape (same discipline
    # as persona_ctx / user_style above).
    guide_ctx = _build_filming_guide_context(filming_guide)
    if guide_ctx:
        all_candidates["filming_guide"] = guide_ctx
    # Narrative clip order (filming-guide alignment). Contract: the first N
    # entries of clip_paths are the guide's shot clips IN GUIDE ORDER — the
    # caller (_dispatch_item_render) derives that ordering. Omit the key when
    # 0/absent so public and legacy jobs keep byte-identical all_candidates
    # shape (same discipline as the optional keys above). The render path
    # additionally gates on NARRATIVE_CLIP_ORDER_ENABLED at render time.
    if narrative_shot_count > 0:
        all_candidates["narrative_shot_count"] = min(int(narrative_shot_count), len(clip_paths))
    # Creator clip notes (dogfood feedback #3): gcs_path → note, only for clips
    # in THIS job and only non-empty notes. Stored for render-time consumers
    # (future intro_writer pickup — deferred pending live-eval budget) and
    # admin/debug. Omit the key entirely when empty (byte-identity discipline).
    if clip_notes:
        notes_ctx = {
            p: str(n)[:200]
            for p, n in clip_notes.items()
            if p in set(clip_paths) and str(n or "").strip()
        }
        if notes_ctx:
            all_candidates["clip_notes"] = notes_ctx
    # Landscape-fit preference (plan-item editor). Only stash when "fit" so
    # public/legacy jobs keep byte-identical all_candidates shape — same omit-
    # when-default discipline used for persona / user_style / filming_guide above.
    if landscape_fit == "fit":
        all_candidates["landscape_fit"] = "fit"
    # Montage visual preset. Omit the default so public/legacy jobs keep their
    # exact all_candidates shape; absent reads as classic at render time.
    resolved_montage_preset = coerce_montage_preset(montage_preset)
    if resolved_montage_preset != DEFAULT_MONTAGE_PRESET:
        all_candidates["montage_preset"] = resolved_montage_preset
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
