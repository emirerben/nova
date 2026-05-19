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
from datetime import UTC, datetime, timedelta

from app.models import TemplateRecipeVersion
from app.tasks import agentic_template_build, template_orchestrate

# ── model shape ─────────────────────────────────────────────────────────────


def test_build_started_at_column_exists_on_model():
    """SQLAlchemy column inspection — independent of an actual DB connection."""
    table = TemplateRecipeVersion.__table__
    assert "build_started_at" in table.columns


def test_build_started_at_is_nullable():
    """Pre-migration rows have no start timestamp to backfill with, and the
    orchestrator might fail before writing. Column must be nullable."""
    col = TemplateRecipeVersion.__table__.columns["build_started_at"]
    assert col.nullable is True


def test_build_started_at_is_timestamptz():
    """Must be timezone-aware (TIMESTAMPTZ in Postgres). All app code writes
    UTC; using a naive TIMESTAMP column would silently drop the tz."""
    col = TemplateRecipeVersion.__table__.columns["build_started_at"]
    # SQLAlchemy stores `timezone=True` on the type for TIMESTAMPTZ.
    assert getattr(col.type, "timezone", False) is True


def test_template_recipe_version_accepts_build_started_at_kwarg():
    """Constructor must accept the field — pins the call sites in both tasks."""
    when = datetime(2026, 5, 16, 10, 0, 0, tzinfo=UTC)
    v = TemplateRecipeVersion(
        template_id="abc",
        recipe={"shot_count": 7, "slots": []},
        trigger="initial_analysis",
        build_started_at=when,
    )
    assert v.build_started_at == when


def test_template_recipe_version_accepts_null_build_started_at():
    """Existing test fixtures or future callers that don't supply the field
    must not crash — nullable means literally optional."""
    v = TemplateRecipeVersion(
        template_id="abc",
        recipe={"shot_count": 7, "slots": []},
        trigger="initial_analysis",
    )
    assert v.build_started_at is None


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


def test_per_run_duration_math():
    """A consumer-side smoke test: subtracting build_started_at from created_at
    yields a duration. This pins the intended consumer use case so it's clear
    why this column exists at all."""
    started = datetime(2026, 5, 16, 10, 0, 0, tzinfo=UTC)
    ended = started + timedelta(seconds=42)
    v = TemplateRecipeVersion(
        template_id="abc",
        recipe={"shot_count": 7, "slots": []},
        trigger="initial_analysis",
        build_started_at=started,
        created_at=ended,
    )
    duration = (v.created_at - v.build_started_at).total_seconds()
    assert duration == 42
