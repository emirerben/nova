"""Recipe-staleness detection for video_templates.

Background: ``video_templates.recipe_cached`` is a frozen JSONB blob written
once per template by ``analyze_template_task`` (manual path) or
``agentic_template_build_task`` (agentic path). Every per-job submission reads
that materialized recipe directly — analysis is NOT re-run on each job. As a
result, bumping an agent's ``prompt_version`` has zero observable effect on
existing templates until somebody explicitly clicks Reanalyze.

This module makes that staleness boundary visible. Each write site calls
``capture_recipe_versions(is_agentic=...)`` and persists the resulting
``{agent_name: prompt_version}`` map alongside the recipe in
``video_templates.recipe_cached_versions``. The admin endpoints call
``diff_recipe_versions(stored, is_agentic=...)`` to compute which agents have
drifted since the recipe was built and surface a STALE badge so the operator
can reanalyze deliberately.

Why two separate canonical lists (manual vs agentic): the manual analyze path
only runs ``TemplateRecipeAgent``. The agentic build runs the recipe agent
plus the text-overlay agent stack (template_text + Layer-2 stages). Tracking
the agentic agents on manual templates would force a false STALE flag every
time a Layer-2 prompt rotates on templates that don't use Layer-2.

Why NULL ``recipe_cached_versions`` is treated as stale: existing rows
predate this column. Treating them as stale on first deploy nudges operators
to reanalyze through the same UI path as a real version drift — no separate
backfill task. The cost is one extra Reanalyze click per active template.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.agents._runtime import AgentSpec


def _manual_template_specs() -> list[AgentSpec]:
    """Agents whose output is baked into a manual template's recipe_cached."""
    from app.agents.template_recipe import TemplateRecipeAgent  # noqa: PLC0415

    return [TemplateRecipeAgent.spec]


def _agentic_template_specs() -> list[AgentSpec]:
    """Agents whose output is baked into an agentic template's recipe_cached.

    Order is for stable diff output; the comparison itself is set-based.
    """
    from app.agents.creative_direction import CreativeDirectionAgent  # noqa: PLC0415
    from app.agents.template_recipe import TemplateRecipeAgent  # noqa: PLC0415
    from app.agents.template_text import TemplateTextAgent  # noqa: PLC0415
    from app.agents.text_alignment import TextAlignmentAgent  # noqa: PLC0415
    from app.agents.text_classification import TextClassificationAgent  # noqa: PLC0415
    from app.agents.text_designer import TextDesignerAgent  # noqa: PLC0415
    from app.agents.transcript import TranscriptAgent  # noqa: PLC0415

    return [
        TemplateRecipeAgent.spec,
        CreativeDirectionAgent.spec,
        TranscriptAgent.spec,
        TemplateTextAgent.spec,
        TextAlignmentAgent.spec,
        TextClassificationAgent.spec,
        TextDesignerAgent.spec,
    ]


def _specs_for(*, is_agentic: bool) -> list[AgentSpec]:
    return _agentic_template_specs() if is_agentic else _manual_template_specs()


def capture_recipe_versions(*, is_agentic: bool) -> dict[str, str]:
    """Read live ``AgentSpec.prompt_version`` values into ``{name: version}``.

    Called at recipe-write time; the result is persisted to
    ``video_templates.recipe_cached_versions``.
    """
    return {spec.name: spec.prompt_version for spec in _specs_for(is_agentic=is_agentic)}


def diff_recipe_versions(
    stored: dict[str, str] | None,
    *,
    is_agentic: bool,
) -> list[str]:
    """Return the sorted agent names whose live prompt_version differs from stored.

    Returns an empty list when the stored map exactly matches the live versions
    for the canonical agent list. Returns the full canonical list when
    ``stored`` is None — see module docstring for the rationale.

    An agent missing from ``stored`` (e.g. a new agent introduced after the
    recipe was built) counts as stale. Extra agents in ``stored`` (e.g. a
    retired agent whose name no longer appears in the canonical list) are
    ignored — their drift can't make the current recipe stale because they
    no longer contribute to it.

    An empty dict (``{}``) is treated as "no LLM agents contributed" — used by
    the audio_only template regen path which rebuilds the recipe purely from
    beat timestamps. Returns an empty drift list so audio_only templates don't
    display a permanent STALE badge.
    """
    current = capture_recipe_versions(is_agentic=is_agentic)
    if stored is None:
        return sorted(current.keys())
    if stored == {}:
        return []
    drifted: list[str] = []
    for name, live_version in current.items():
        if stored.get(name) != live_version:
            drifted.append(name)
    return sorted(drifted)


def is_recipe_stale(
    stored: dict[str, str] | None,
    *,
    is_agentic: bool,
) -> bool:
    """True if any canonical agent's live prompt_version differs from stored."""
    return len(diff_recipe_versions(stored, is_agentic=is_agentic)) > 0
