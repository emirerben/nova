"""Tests for app.services.template_staleness.

These tests pin the staleness semantics that the admin UI depends on. The
business rule is: an existing template materialized its recipe at some past
moment, and we want the admin UI to flag it as STALE when any of the canonical
agents has rotated its prompt_version since.

Two value sentinels matter:
- ``None`` (NULL on the row) means "we have no record" — every active agent
  is reported drifted so pre-migration rows show STALE and surface for
  reanalysis on first deploy.
- ``{}`` (empty dict) means "no LLM agents contributed" (audio_only regen
  path). The diff returns no drift so those rows don't display a permanent
  STALE badge.

``AgentSpec`` is a frozen dataclass — we can't ``monkeypatch.setattr`` its
fields. Instead each behavioural test patches ``_specs_for`` to return
duck-typed stubs with stable (name, prompt_version) values so expectations
don't break the next time a real prompt rotates. A separate shape test still
asserts that the real canonical lists contain the expected agent names so we
catch "forgot to add the new agent" regressions.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.services import template_staleness
from app.services.template_staleness import (
    capture_recipe_versions,
    diff_recipe_versions,
    is_recipe_stale,
)


@dataclass
class _SpecStub:
    """Minimal AgentSpec stand-in (name + prompt_version only)."""

    name: str
    prompt_version: str


@pytest.fixture
def stub_specs(monkeypatch):
    """Replace the canonical agent lists with deterministic stubs."""
    manual = [_SpecStub("nova.compose.template_recipe", "v1")]
    agentic = [
        _SpecStub("nova.compose.template_recipe", "v1"),
        _SpecStub("nova.compose.creative_direction", "v1"),
        _SpecStub("nova.audio.transcript", "v1"),
        _SpecStub("nova.compose.template_text", "v1"),
        _SpecStub("nova.compose.text_alignment", "v1"),
        _SpecStub("nova.compose.text_classification", "v1"),
        _SpecStub("nova.layout.text_designer", "v1"),
    ]

    def fake_specs(*, is_agentic):
        return agentic if is_agentic else manual

    monkeypatch.setattr(template_staleness, "_specs_for", fake_specs)
    return {"manual": manual, "agentic": agentic}


def test_capture_returns_manual_agent_only(stub_specs):
    assert capture_recipe_versions(is_agentic=False) == {
        "nova.compose.template_recipe": "v1",
    }


def test_capture_returns_full_agentic_agent_set(stub_specs):
    assert capture_recipe_versions(is_agentic=True) == {
        "nova.compose.template_recipe": "v1",
        "nova.compose.creative_direction": "v1",
        "nova.audio.transcript": "v1",
        "nova.compose.template_text": "v1",
        "nova.compose.text_alignment": "v1",
        "nova.compose.text_classification": "v1",
        "nova.layout.text_designer": "v1",
    }


def test_diff_empty_when_stored_matches_live(stub_specs):
    stored = capture_recipe_versions(is_agentic=True)
    assert diff_recipe_versions(stored, is_agentic=True) == []
    assert is_recipe_stale(stored, is_agentic=True) is False


def test_diff_reports_single_agent_when_one_prompt_rotated(stub_specs):
    stored = capture_recipe_versions(is_agentic=True)
    # Operator bumps one agent's prompt — only that name should appear.
    stub_specs["agentic"][4] = _SpecStub("nova.compose.text_alignment", "v2")

    drift = diff_recipe_versions(stored, is_agentic=True)
    assert drift == ["nova.compose.text_alignment"]
    assert is_recipe_stale(stored, is_agentic=True) is True


def test_diff_reports_missing_agent_as_drifted(stub_specs):
    # Recipe written before a new agent existed — that agent is "drifted"
    # because we have no record of which version produced the output.
    stored = capture_recipe_versions(is_agentic=True)
    del stored["nova.compose.text_alignment"]

    drift = diff_recipe_versions(stored, is_agentic=True)
    assert drift == ["nova.compose.text_alignment"]


def test_diff_ignores_extra_stored_agents(stub_specs):
    # A retired agent that no longer appears in the canonical list must not
    # keep flagging recipes as stale — its drift cannot affect current output.
    stored = capture_recipe_versions(is_agentic=True)
    stored["nova.retired.legacy_agent"] = "ancient"

    assert diff_recipe_versions(stored, is_agentic=True) == []


def test_diff_with_none_returns_full_agent_set(stub_specs):
    # Legacy/pre-migration rows have NULL recipe_cached_versions. They must
    # surface as STALE so the operator reanalyzes them.
    drift = diff_recipe_versions(None, is_agentic=True)
    assert drift == sorted(
        [
            "nova.audio.transcript",
            "nova.compose.creative_direction",
            "nova.compose.template_recipe",
            "nova.compose.template_text",
            "nova.compose.text_alignment",
            "nova.compose.text_classification",
            "nova.layout.text_designer",
        ]
    )


def test_diff_with_empty_dict_returns_no_drift(stub_specs):
    # Audio-only regen explicitly writes {} to signal "no LLM agents
    # contributed". Those rows must not display a permanent STALE badge.
    assert diff_recipe_versions({}, is_agentic=True) == []
    assert diff_recipe_versions({}, is_agentic=False) == []
    assert is_recipe_stale({}, is_agentic=True) is False


def test_manual_path_ignores_agentic_agent_drift(stub_specs):
    # Manual templates only bake TemplateRecipeAgent output — rotating a
    # Layer-2 prompt must not flag manual recipes as stale.
    stub_specs["agentic"][4] = _SpecStub("nova.compose.text_alignment", "v2")
    stored = {"nova.compose.template_recipe": "v1"}

    assert diff_recipe_versions(stored, is_agentic=False) == []


def test_manual_path_flags_recipe_agent_drift(stub_specs):
    stored = capture_recipe_versions(is_agentic=False)
    stub_specs["manual"][0] = _SpecStub("nova.compose.template_recipe", "v2")

    drift = diff_recipe_versions(stored, is_agentic=False)
    assert drift == ["nova.compose.template_recipe"]


# ── Canonical-list shape (operates on the REAL specs, no patching) ──────────


def test_canonical_manual_list_contains_template_recipe_agent():
    """Catches accidental removal of TemplateRecipeAgent from the manual list."""
    specs = template_staleness._manual_template_specs()
    names = {s.name for s in specs}
    assert "nova.compose.template_recipe" in names


def test_canonical_agentic_list_contains_all_text_overlay_agents():
    """Catches forgetting to add a new text-overlay agent to the canonical
    list — the same class of bug that caused the original silent staleness."""
    specs = template_staleness._agentic_template_specs()
    names = {s.name for s in specs}
    # Each of these contributes to the materialized agentic recipe in some way:
    # recipe structure, creative direction, transcript input, Layer-1 text,
    # Layer-2 alignment, Layer-2 classification, per-slot text styling.
    assert names >= {
        "nova.compose.template_recipe",
        "nova.compose.creative_direction",
        "nova.audio.transcript",
        "nova.compose.template_text",
        "nova.compose.text_alignment",
        "nova.compose.text_classification",
        "nova.layout.text_designer",
    }
