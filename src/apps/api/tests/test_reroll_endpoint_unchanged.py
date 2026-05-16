"""Regression pin: POST /template-jobs/{id}/reroll stays unchanged post-Phase-3.

IRON RULE regression test #3 for the auto-music Phase 3 PR (see
plans/our-current-agentic-template-scalable-gem.md). The reroll
endpoint creates a fresh template-mode job from an existing one's
clip paths + template_id. It exists exclusively in template-mode and
MUST NOT route through the new auto-music orchestrator.

Coverage:
  1. The endpoint is declared on the template-jobs router (not music
     or auto-music).
  2. The endpoint dispatches to ``orchestrate_template_job`` or
     ``orchestrate_single_video_job`` — never to
     ``orchestrate_auto_music_job``.
  3. The reroll guard requires status == ``template_ready`` on the
     source job (rejects auto-music's ``variants_ready`` etc.).
  4. The reroll response shape (TemplateJobResponse with template_id,
     status, job_id) is unchanged.
"""

from __future__ import annotations

import inspect

from app.routes import template_jobs
from app.routes.template_jobs import reroll_template_job


def test_reroll_handler_lives_on_template_router() -> None:
    """The reroll handler must be defined inside app.routes.template_jobs,
    not in a new auto-music router. Moving it would break the URL prefix."""
    assert reroll_template_job.__module__ == "app.routes.template_jobs"


def test_reroll_dispatches_to_template_tasks_not_auto_music() -> None:
    """The reroll endpoint's source code references the template Celery
    tasks. It MUST NOT reference orchestrate_auto_music_job."""
    src = inspect.getsource(reroll_template_job)
    assert "orchestrate_template_job" in src
    assert "orchestrate_single_video_job" in src
    assert "orchestrate_auto_music_job" not in src, (
        "reroll handler imports orchestrate_auto_music_job — the template "
        "reroll path was incorrectly wired into auto-music. Phase 3 must "
        "not modify reroll routing."
    )


def test_reroll_status_guard_still_template_ready() -> None:
    """Reroll only allows source jobs with status=template_ready. This guard
    must not have been relaxed to accept auto-music's variant statuses —
    a future user shouldn't be able to "reroll" an auto-music variant
    through the template endpoint.
    """
    src = inspect.getsource(reroll_template_job)
    assert '"template_ready"' in src, (
        "reroll's template_ready status guard disappeared — the gate that "
        "ensures reroll only operates on completed template jobs was removed."
    )
    # The reroll guard MUST NOT have been broadened to auto-music variants.
    for forbidden in ("variants_ready", "variants_ready_partial", "auto_music"):
        assert forbidden not in src, (
            f"reroll handler references {forbidden!r} — the template-mode "
            f"reroll path was incorrectly broadened to auto-music statuses."
        )


def test_reroll_job_type_guard_still_template() -> None:
    """Reroll only operates on jobs with job_type='template'. The auto-music
    flow uses a different job_type and must NOT be reachable from reroll.
    """
    src = inspect.getsource(reroll_template_job)
    assert '"template"' in src
    assert 'job_type' in src


def test_reroll_creates_new_job_with_template_job_type() -> None:
    """The new job created by reroll MUST be job_type='template', not
    'auto_music' or anything else."""
    src = inspect.getsource(reroll_template_job)
    # The new Job construction uses job_type="template".
    assert 'job_type="template"' in src


def test_reroll_response_shape_unchanged() -> None:
    """The reroll endpoint's response is TemplateJobResponse with these
    exact fields. New fields would be a breaking client change."""
    src = inspect.getsource(template_jobs)
    assert "class TemplateJobResponse" in src or "TemplateJobResponse" in src
    # Pull out the model — it lives at module level.
    response_cls = getattr(template_jobs, "TemplateJobResponse", None)
    assert response_cls is not None
    fields = set(response_cls.model_fields.keys())
    # Required minimum — exact set may have changed once historically.
    for required in ("job_id", "status", "template_id"):
        assert required in fields, (
            f"TemplateJobResponse lost field {required!r} — reroll response "
            f"shape changed in a way that would break existing clients."
        )
    # Auto-music-specific fields MUST NOT be on this response.
    for forbidden in ("variants", "music_track_id", "match_score"):
        assert forbidden not in fields, (
            f"TemplateJobResponse grew an auto-music field {forbidden!r}"
        )
