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


def build_generative_job(
    *,
    user_id: uuid.UUID,
    clip_paths: list[str],
    mode: str = "generative",
    language: str = "en",
    selected_platforms: list[str] | None = None,
    content_plan_item_id: uuid.UUID | None = None,
) -> Job:
    """Construct (not persist) a generative Job after validating clip prefixes.

    Raises ValueError if any clip path is outside the allowlist — callers map
    that to a 422. `mode` is "generative" for the public flow and "content_plan"
    for per-item plan generation; the render path (`orchestrate_generative_job`)
    is identical, `content_plan_item_id` is just the reverse link for admin/debug.
    """
    if not clip_paths:
        raise ValueError("At least 1 clip is required")
    _validate_clip_path_prefixes(clip_paths)
    return Job(
        user_id=user_id,
        job_type="generative",
        mode=mode,
        raw_storage_path=clip_paths[0],
        selected_platforms=selected_platforms or list(DEFAULT_PLATFORMS),
        all_candidates={"clip_paths": clip_paths, "language": language},
        content_plan_item_id=content_plan_item_id,
        status="queued",
    )
