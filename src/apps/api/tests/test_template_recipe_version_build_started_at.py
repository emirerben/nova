"""Tests for the build_started_at field on TemplateRecipeVersion.

Paired with the DB-generated `created_at`, this field gives per-run
template-analysis wall-clock without Langfuse. The field must:

  - Exist on the model with TIMESTAMPTZ + nullable.
  - Be wired at task entry in both `agentic_template_build_task` and
    `analyze_template_task` so the value flows into the row.
  - Tolerate NULL on read (rows written by pre-migration code).
"""

from __future__ import annotations

import inspect

from app.models import TemplateRecipeVersion
from app.tasks import agentic_template_build, template_orchestrate

# ── model shape ─────────────────────────────────────────────────────────────


def test_build_started_at_is_timestamptz():
    """Must be timezone-aware (TIMESTAMPTZ in Postgres). All app code writes
    UTC; using a naive TIMESTAMP column would silently drop the tz."""
    col = TemplateRecipeVersion.__table__.columns["build_started_at"]
    # SQLAlchemy stores `timezone=True` on the type for TIMESTAMPTZ.
    assert getattr(col.type, "timezone", False) is True


# ── task-side wiring contract ───────────────────────────────────────────────


def test_agentic_build_task_captures_build_started_at():
    """The agentic build task must capture the timestamp at entry and pass it
    to the TemplateRecipeVersion constructor. Source-inspection pin so a
    future refactor that drops the assignment is caught loudly.

    (Same rationale as the Phase 3/4 contract tests in test_template_cache.py
    and test_clip_router_cache.py — mocking the full task dependency chain
    for one structural assertion is more brittle than this pin.)"""
    src = inspect.getsource(agentic_template_build.agentic_template_build_task)
    assert "build_started_at = datetime.now(UTC)" in src, (
        "agentic_template_build_task must capture build_started_at at task entry"
    )
    assert "build_started_at=build_started_at" in src, (
        "agentic_template_build_task must pass build_started_at to TemplateRecipeVersion"
    )


def test_manual_analyze_template_task_captures_build_started_at():
    """Same contract for the manual path."""
    src = inspect.getsource(template_orchestrate.analyze_template_task)
    assert "build_started_at = datetime.now(UTC)" in src, (
        "analyze_template_task must capture build_started_at at task entry"
    )
    assert "build_started_at=build_started_at" in src, (
        "analyze_template_task must pass build_started_at to TemplateRecipeVersion"
    )


# ── per-run duration math (the whole point) ─────────────────────────────────
