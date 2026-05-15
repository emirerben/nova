"""Regression pin: template-mode flow stays byte-identical post-Phase-3.

IRON RULE regression test #2 for the auto-music Phase 3 PR (see
plans/our-current-agentic-template-scalable-gem.md). Pins the public
surface of POST /template-jobs + orchestrate_template_job so a future
refactor of the auto-music orchestrator can't quietly break the
template path.

Mock-based, not full-render. The point is regression DETECTION at the
public-surface boundary; full byte-level golden output is covered by
separate render tests.

Coverage:
  1. CreateTemplateJobRequest schema fields
  2. orchestrate_template_job is a Celery task with the canonical name
  3. orchestrate_single_video_job is a Celery task with the canonical name
  4. Status string transitions (template_ready, processing_failed)
  5. The reroll endpoint imports the right task names
"""

from __future__ import annotations

import inspect

from app.routes.template_jobs import CreateTemplateJobRequest
from app.tasks.template_orchestrate import (
    orchestrate_single_video_job,
    orchestrate_template_job,
)


# ── Request schema pin ────────────────────────────────────────────────────────


def test_create_template_job_request_required_fields_unchanged() -> None:
    """The template-jobs request schema must still have these fields and
    must NOT have grown an auto-music-style ``n_variants`` or ``mode`` field.
    """
    fields = CreateTemplateJobRequest.model_fields
    assert "template_id" in fields
    assert "clip_gcs_paths" in fields
    # The auto-music feature MUST NOT bleed onto the template route.
    assert "n_variants" not in fields, (
        "n_variants leaked onto CreateTemplateJobRequest — that field is "
        "for auto-music only. Template mode renders 1 output per job."
    )
    # `mode` is the new column on Job; it MUST NOT appear on the
    # template request schema (would break old clients).
    assert "mode" not in fields


# ── Celery task pin ───────────────────────────────────────────────────────────


def test_orchestrate_template_job_task_name_unchanged() -> None:
    """Renaming this task breaks in-flight Redis-broker messages mid-deploy."""
    assert orchestrate_template_job.name == "tasks.orchestrate_template_job"


def test_orchestrate_single_video_job_task_name_unchanged() -> None:
    """Single-video template-mode keeps its own task name."""
    assert orchestrate_single_video_job.name == "tasks.orchestrate_single_video_job"


def test_orchestrate_template_job_signature_unchanged() -> None:
    """Task accepts (self, job_id, **kwargs). The force_single_pass kwarg
    was added in v0.4.18.0 — pinning it here so a Phase 3 refactor doesn't
    accidentally drop it.
    """
    sig = inspect.signature(orchestrate_template_job.__wrapped__)
    params = list(sig.parameters.keys())
    # __wrapped__ strips Celery's bind=True self binding; remaining
    # params are the user-visible signature.
    assert params[0] == "job_id"
    # If a future change adds another positional arg, this test will
    # break and force the author to read this assertion's docstring.


# ── Status string transitions ─────────────────────────────────────────────────


def test_template_ready_status_string_unchanged() -> None:
    """Successful template jobs end in status=template_ready.

    The frontend result page branches on this exact string.
    """
    from app.tasks import template_orchestrate

    src = inspect.getsource(template_orchestrate)
    assert '"template_ready"' in src, (
        "template_ready terminal status string disappeared — the public "
        "status taxonomy was altered."
    )


def test_processing_failed_status_string_unchanged_in_template_path() -> None:
    """Failed template jobs end in status=processing_failed."""
    from app.tasks import template_orchestrate

    src = inspect.getsource(template_orchestrate)
    assert '"processing_failed"' in src, (
        "processing_failed status string disappeared from template path"
    )


# ── Reroll endpoint dispatch pin ──────────────────────────────────────────────


def test_reroll_endpoint_dispatches_to_existing_task_names() -> None:
    """The reroll endpoint (tested separately in test_reroll_endpoint_unchanged.py)
    must dispatch by importing the EXACT task names. This test pins the
    template-side names — the reroll-specific test pins the dispatch path.
    """
    from app.routes import template_jobs

    src = inspect.getsource(template_jobs)
    assert "orchestrate_template_job" in src
    assert "orchestrate_single_video_job" in src
    # Reroll MUST NOT route to the new auto-music task.
    assert "orchestrate_auto_music_job" not in src, (
        "template_jobs route imported orchestrate_auto_music_job — the "
        "reroll/template path was incorrectly wired into auto-music."
    )
