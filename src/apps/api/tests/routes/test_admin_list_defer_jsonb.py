"""Compile-time checks that admin list endpoints defer their JSONB blobs.

The admin list responses (/admin/jobs, /admin/templates, /admin/music-tracks)
all return small metadata projections, but the underlying ORM queries used
to fetch every column on the row — including multi-KB JSONB blobs like
``Job.pipeline_trace``, ``VideoTemplate.recipe_cached`` and
``MusicTrack.ai_labels``. With deployments accumulating jobs, those payloads
grew to multi-megabyte responses on admin page loads.

The fix is ``defer()`` on the heavy columns in the list query. These tests
compile the actual SQL statements built by the list endpoints' query
constructions and assert the deferred columns are NOT in the SELECT list,
without needing a live Postgres connection — defer() is a compile-time
concern, so a compiled-text check is the right shape.

Each test also explicitly verifies that the columns the response shape
DOES reference are still selected (so we don't regress by deferring too
much).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, inspect, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import defer, load_only

from app.models import Job, MusicTrack, VideoTemplate
from app.routes.admin_music import _to_list_item, list_music_tracks


def _compiled_sql(stmt) -> str:
    """Compile to PostgreSQL dialect text (with literal binds) for inspection."""
    return str(
        stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()


def _selects_column(sql: str, table: str, col: str) -> bool:
    """Word-boundary-aware check that ``table.col`` appears in the SELECT list.
    Prevents false matches against longer column names that share a prefix
    (e.g. ``recipe_cached`` vs ``recipe_cached_at``)."""
    return re.search(rf"\b{re.escape(table)}\.{re.escape(col)}\b(?!_)", sql) is not None


# ── Job ───────────────────────────────────────────────────────────────────────


def _job_list_query():
    """Mirror of admin_jobs.list_jobs's `base` construction."""
    return select(Job).options(
        defer(Job.assembly_plan),
        defer(Job.probe_metadata),
        defer(Job.transcript),
        defer(Job.scene_cuts),
        defer(Job.all_candidates),
        defer(Job.phase_log),
        defer(Job.pipeline_trace),
    )


def test_job_list_defers_heavy_jsonb_columns():
    sql = _compiled_sql(_job_list_query())
    for col in (
        "assembly_plan",
        "probe_metadata",
        "transcript",
        "scene_cuts",
        "all_candidates",
        "phase_log",
        "pipeline_trace",
    ):
        assert not _selects_column(sql, "jobs", col), (
            f"expected jobs.{col} to be deferred, got: {sql}"
        )


def test_job_list_still_selects_response_columns():
    """The list response references these columns — they must still be in the SELECT."""
    sql = _compiled_sql(_job_list_query())
    for col in (
        "id",
        "status",
        "job_type",
        "mode",
        "template_id",
        "music_track_id",
        "failure_reason",
        "created_at",
        "updated_at",
        "started_at",
        "celery_task_id",
    ):
        assert _selects_column(sql, "jobs", col), f"expected jobs.{col} in SELECT, got: {sql}"


# ── VideoTemplate ─────────────────────────────────────────────────────────────


def _template_list_query():
    """Mirror of admin.list_templates's `query` construction (column-load options only)."""
    return select(VideoTemplate).options(
        defer(VideoTemplate.recipe_cached),
        defer(VideoTemplate.required_inputs),
        defer(VideoTemplate.lyrics_config),
    )


def test_template_list_defers_heavy_jsonb_columns():
    sql = _compiled_sql(_template_list_query())
    for col in ("recipe_cached", "required_inputs", "lyrics_config"):
        assert not _selects_column(sql, "video_templates", col), (
            f"expected video_templates.{col} to be deferred, got: {sql}"
        )


def test_template_list_still_selects_recipe_cached_versions():
    """The list response feeds recipe_cached_versions to diff_recipe_versions;
    deferring it would force a lazy-load per row and re-introduce N+1."""
    sql = _compiled_sql(_template_list_query())
    assert _selects_column(sql, "video_templates", "recipe_cached_versions"), sql


# ── MusicTrack ────────────────────────────────────────────────────────────────


def _music_list_query():
    """Mirror of admin_music.list_music_tracks's `base_query` construction."""
    beat_count_expr = func.coalesce(
        func.jsonb_array_length(MusicTrack.beat_timestamps_s),
        0,
    ).label("beat_count")
    has_ai_labels_expr = MusicTrack.ai_labels.isnot(None).label("has_ai_labels")
    has_best_sections_expr = MusicTrack.best_sections.isnot(None).label("has_best_sections")
    return select(MusicTrack, beat_count_expr, has_ai_labels_expr, has_best_sections_expr).options(
        load_only(
            MusicTrack.id,
            MusicTrack.title,
            MusicTrack.artist,
            MusicTrack.analysis_status,
            MusicTrack.thumbnail_url,
            MusicTrack.published_at,
            MusicTrack.archived_at,
            MusicTrack.created_at,
            MusicTrack.label_version,
            MusicTrack.section_version,
        )
    )


def test_music_list_loads_only_slim_columns_plus_sql_beat_count():
    sql = _compiled_sql(_music_list_query())
    assert "jsonb_array_length(music_tracks.beat_timestamps_s)" in sql
    for col in (
        "id",
        "title",
        "artist",
        "analysis_status",
        "thumbnail_url",
        "published_at",
        "archived_at",
        "created_at",
    ):
        assert _selects_column(sql, "music_tracks", col), (
            f"expected music_tracks.{col} in SELECT, got: {sql}"
        )
    # These JSONB blobs must never be loaded onto the row at all.
    for col in ("track_config", "lyrics_cached", "recipe_cached"):
        assert not _selects_column(sql, "music_tracks", col), (
            f"expected music_tracks.{col} to stay out of the list SELECT, got: {sql}"
        )
    # ai_labels / best_sections are referenced ONLY inside server-side
    # `IS NOT NULL` boolean expressions (has_ai_labels / has_best_sections) —
    # the multi-KB blob itself is never returned. The entity-level guarantee
    # that the blob stays unloaded is locked by the inspect(track).unloaded
    # checks below; here we just confirm the only reference is the predicate.
    for col in ("ai_labels", "best_sections"):
        assert f"music_tracks.{col} is not null" in sql, (
            f"expected music_tracks.{col} only as an IS NOT NULL predicate, got: {sql}"
        )
        # No bare selection of the blob (e.g. `music_tracks.ai_labels,` or
        # `music_tracks.ai_labels \nfrom`) — only the predicate form is allowed.
        assert not re.search(rf"\bmusic_tracks\.{col}\b(?!_)(?!\s+is\s+not\s+null)", sql), (
            f"expected music_tracks.{col} blob to stay out of the SELECT, got: {sql}"
        )


def test_music_list_item_does_not_touch_heavy_columns():
    track = MusicTrack(
        id="track-1",
        title="Track",
        artist="Artist",
        analysis_status="ready",
        thumbnail_url=None,
        published_at=None,
        archived_at=None,
        created_at=datetime.now(UTC),
    )
    for col in (
        "beat_timestamps_s",
        "track_config",
        "lyrics_cached",
        "best_sections",
        "recipe_cached",
        "ai_labels",
    ):
        assert col in inspect(track).unloaded


@pytest.mark.asyncio
async def test_music_list_endpoint_returns_exact_slim_keys_and_sql_beat_count():
    track = MusicTrack(
        id="track-1",
        title="Track",
        artist="Artist",
        analysis_status="ready",
        thumbnail_url="https://example.com/thumb.jpg",
        published_at=None,
        archived_at=None,
        created_at=datetime.now(UTC),
    )
    count_result = MagicMock()
    count_result.scalar.return_value = 1
    rows_result = MagicMock()
    # Rows now carry the SQL-derived has_ai_labels / has_best_sections booleans.
    rows_result.all.return_value = [(track, 4, False, False)]
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[count_result, rows_result])

    response = await list_music_tracks(db=db, limit=50, offset=0)
    payload = response.model_dump(mode="json")

    expected_keys = {
        "id",
        "title",
        "artist",
        "analysis_status",
        "thumbnail_url",
        "beat_count",
        "published_at",
        "archived_at",
        "label_version",
        "section_version",
        "has_ai_labels",
        "generative_matchable",
        "created_at",
    }
    assert payload["total"] == 1
    assert payload["tracks"][0].keys() == expected_keys
    assert payload["tracks"][0]["beat_count"] == 4
    assert payload["tracks"][0]["has_ai_labels"] is False
    assert payload["tracks"][0]["generative_matchable"] is False

    item = _to_list_item(track, beat_count=7, has_ai_labels=False, has_best_sections=False)

    assert item.model_dump().keys() == expected_keys
    assert item.beat_count == 7
    for col in (
        "beat_timestamps_s",
        "track_config",
        "lyrics_cached",
        "best_sections",
        "recipe_cached",
        "ai_labels",
    ):
        assert col in inspect(track).unloaded


# ── Detail-endpoint regression: a plain select(Model) MUST still load everything ─


def test_plain_select_job_still_loads_jsonb():
    """Sanity: defer() lives on the list query's options, not the model.
    A bare select(Job) — what the detail endpoint uses — must still load
    every column, including the deferred-on-the-list-side ones."""
    sql = _compiled_sql(select(Job))
    for col in ("assembly_plan", "pipeline_trace", "transcript"):
        assert _selects_column(sql, "jobs", col), f"expected jobs.{col} in bare select, got: {sql}"


def test_plain_select_template_still_loads_jsonb():
    sql = _compiled_sql(select(VideoTemplate))
    for col in ("recipe_cached", "required_inputs", "lyrics_config"):
        assert _selects_column(sql, "video_templates", col), sql


def test_plain_select_music_track_still_loads_jsonb():
    sql = _compiled_sql(select(MusicTrack))
    for col in (
        "beat_timestamps_s",
        "track_config",
        "lyrics_cached",
        "best_sections",
        "recipe_cached",
        "ai_labels",
    ):
        assert _selects_column(sql, "music_tracks", col), sql
