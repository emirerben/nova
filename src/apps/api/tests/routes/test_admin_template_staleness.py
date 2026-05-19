"""Routes-layer wiring tests for the staleness signal.

The deep semantics live in tests/services/test_template_staleness.py. This
file pins that _template_response actually surfaces the field into the API
response — a regression here would silently disable the admin badge.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.routes.admin import _template_response
from app.services import template_staleness


@pytest.fixture
def stub_specs(monkeypatch):
    """Pin the canonical list to a known shape so the assertion below
    doesn't break every time a real prompt rotates."""
    from dataclasses import dataclass

    @dataclass
    class Spec:
        name: str
        prompt_version: str

    agentic = [
        Spec("nova.compose.template_recipe", "v1"),
        Spec("nova.compose.template_text", "v1"),
        Spec("nova.compose.text_alignment", "v1"),
    ]
    manual = [Spec("nova.compose.template_recipe", "v1")]

    def fake_specs(*, is_agentic):
        return agentic if is_agentic else manual

    monkeypatch.setattr(template_staleness, "_specs_for", fake_specs)


def _fake_template(**overrides) -> SimpleNamespace:
    base = dict(
        id="t-123",
        name="Test Template",
        gcs_path="templates/test.mp4",
        analysis_status="ready",
        required_clips_min=5,
        required_clips_max=10,
        required_inputs=[],
        published_at=None,
        archived_at=None,
        description=None,
        source_url=None,
        thumbnail_gcs_path=None,
        error_detail=None,
        template_type="standard",
        parent_template_id=None,
        music_track_id=None,
        is_agentic=True,
        use_layer2_default=None,
        created_at=datetime(2026, 5, 1, tzinfo=UTC),
        recipe_cached=None,
        recipe_cached_versions=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_response_marks_legacy_null_row_as_stale(stub_specs):
    # Pre-migration row → NULL recipe_cached_versions → all agents drift.
    resp = _template_response(_fake_template(recipe_cached_versions=None))
    assert resp.recipe_stale_agents == [
        "nova.compose.template_recipe",
        "nova.compose.template_text",
        "nova.compose.text_alignment",
    ]


def test_response_marks_matching_snapshot_as_fresh(stub_specs):
    resp = _template_response(
        _fake_template(
            recipe_cached_versions={
                "nova.compose.template_recipe": "v1",
                "nova.compose.template_text": "v1",
                "nova.compose.text_alignment": "v1",
            }
        )
    )
    assert resp.recipe_stale_agents == []


def test_response_marks_partial_drift(stub_specs):
    resp = _template_response(
        _fake_template(
            recipe_cached_versions={
                "nova.compose.template_recipe": "v1",
                "nova.compose.template_text": "v1",
                "nova.compose.text_alignment": "v0-old",  # rotated since
            }
        )
    )
    assert resp.recipe_stale_agents == ["nova.compose.text_alignment"]


def test_audio_only_row_is_never_stale(stub_specs):
    # Empty-dict sentinel = "no LLM agents contributed" → never stale even
    # when the canonical list rotates.
    resp = _template_response(_fake_template(template_type="audio_only", recipe_cached_versions={}))
    assert resp.recipe_stale_agents == []


def test_manual_template_ignores_agentic_drift(stub_specs):
    # Manual recipe has only the recipe agent — drift of Layer-2 agents must
    # not flag it stale.
    resp = _template_response(
        _fake_template(
            is_agentic=False,
            recipe_cached_versions={"nova.compose.template_recipe": "v1"},
        )
    )
    assert resp.recipe_stale_agents == []
