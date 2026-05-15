"""Tests for _apply_font_default_to_overlays in agentic_template_build.

This helper bridges PR2's font_identification output (recipe.font_default,
overlay.font_alternatives) into the renderer's resolution chain. The renderer
reads `overlay.font_family → overlay.font_style → registry default`, NOT
`recipe.font_default` — so without this step, agentic templates with a
populated font_default still render in Playfair Display (the registry
fallback). This test pins the contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.tasks.agentic_template_build import _apply_font_default_to_overlays


@dataclass
class _StubRecipe:
    """Minimal stand-in for the TemplateRecipe dataclass.

    Mirrors only the fields the helper reads: slots, font_default.
    """

    slots: list[dict[str, Any]] = field(default_factory=list)
    font_default: str = ""


def _make_overlay(**kwargs) -> dict[str, Any]:
    base = {"role": "label", "sample_text": "hello", "start_s": 0.0, "end_s": 2.0}
    base.update(kwargs)
    return base


def test_bakes_font_default_into_unset_overlays() -> None:
    recipe = _StubRecipe(
        font_default="Inter Tight",
        slots=[
            {"text_overlays": [_make_overlay(), _make_overlay()]},
            {"text_overlays": [_make_overlay()]},
        ],
    )
    applied = _apply_font_default_to_overlays(recipe)
    assert applied == 3
    assert recipe.slots[0]["text_overlays"][0]["font_family"] == "Inter Tight"
    assert recipe.slots[0]["text_overlays"][1]["font_family"] == "Inter Tight"
    assert recipe.slots[1]["text_overlays"][0]["font_family"] == "Inter Tight"


def test_does_not_override_existing_font_family() -> None:
    """A human-authored or text_designer-chosen font_family must survive a
    re-analysis. The bake only fills holes."""
    recipe = _StubRecipe(
        font_default="Inter Tight",
        slots=[
            {
                "text_overlays": [
                    _make_overlay(font_family="Permanent Marker"),
                    _make_overlay(),
                ]
            }
        ],
    )
    applied = _apply_font_default_to_overlays(recipe)
    assert applied == 1
    assert recipe.slots[0]["text_overlays"][0]["font_family"] == "Permanent Marker"
    assert recipe.slots[0]["text_overlays"][1]["font_family"] == "Inter Tight"


def test_no_op_when_font_default_is_empty() -> None:
    """No font_default means font ID either didn't run or returned no above-
    floor matches. Don't fabricate a font for the renderer to use; let the
    existing fallback chain (font_style → Playfair) handle it."""
    recipe = _StubRecipe(
        font_default="",
        slots=[{"text_overlays": [_make_overlay(), _make_overlay()]}],
    )
    applied = _apply_font_default_to_overlays(recipe)
    assert applied == 0
    for ov in recipe.slots[0]["text_overlays"]:
        assert "font_family" not in ov


def test_no_op_when_font_default_is_none() -> None:
    """font_default being None (vs empty string) shouldn't crash. The
    dataclass default is "", but defensive against future schema drift."""
    recipe = _StubRecipe(slots=[{"text_overlays": [_make_overlay()]}])
    recipe.font_default = None  # type: ignore[assignment]
    applied = _apply_font_default_to_overlays(recipe)
    assert applied == 0


def test_handles_recipe_with_no_slots() -> None:
    recipe = _StubRecipe(font_default="Inter Tight", slots=[])
    assert _apply_font_default_to_overlays(recipe) == 0


def test_handles_slots_with_no_text_overlays() -> None:
    """Some slots have no overlays at all (raw b-roll). Don't crash on missing
    or empty text_overlays."""
    recipe = _StubRecipe(
        font_default="Inter Tight",
        slots=[
            {},  # no text_overlays key
            {"text_overlays": []},  # empty list
            {"text_overlays": None},  # explicit null
            {"text_overlays": [_make_overlay()]},
        ],
    )
    applied = _apply_font_default_to_overlays(recipe)
    assert applied == 1
    assert recipe.slots[3]["text_overlays"][0]["font_family"] == "Inter Tight"


def test_idempotent_on_repeated_calls() -> None:
    """Re-running the bake (e.g. on a re-analysis) doesn't double-apply."""
    recipe = _StubRecipe(
        font_default="Inter Tight",
        slots=[{"text_overlays": [_make_overlay()]}],
    )
    assert _apply_font_default_to_overlays(recipe) == 1
    # Second call sees the field already populated; counts 0 applied.
    assert _apply_font_default_to_overlays(recipe) == 0
    assert recipe.slots[0]["text_overlays"][0]["font_family"] == "Inter Tight"
