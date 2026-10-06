"""Parity: the editor's projected intro element must carry the look the burn used.

`_resolve_intro_overlay_params` resolves the intro face, effect, colors and
stroke from creator override > user-style knob > curated style set, but only
the creator overrides persist as `intro_*` fields. The read adapter
(`_base_text_elements_for_variant`) therefore projected every montage /
talking-head intro as a Playfair karaoke line with no face, and the first text
Save (web or iOS) reburned it through `_text_element_burn_dicts` in that look
instead of the set's (Bodoni Moda, Fraunces, Space Mono...).

The invariant pinned here: for a persisted `style_set_id` (+ overrides/knobs),
the burn dicts the ADAPTER builds carry the same look as the burn dicts the
RENDER produced, and the saved element's settled frame burns pixel-identically.
Placement parity lives in test_intro_placement_parity.py.
"""

from __future__ import annotations

import io
import types

import numpy as np
import pytest
from PIL import Image

import app.tasks.generative_build as gb
from app.agents._schemas.text_element import text_elements_for_variant
from app.pipeline import text_overlay_skia as tos
from app.pipeline.generative_overlays import (
    build_overlays_from_text_elements,
    build_persistent_intro_overlays,
)
from app.pipeline.style_sets import resolve_overlay_style, style_set_ids

_LOOK_KEYS = (
    "font_family",
    "effect",
    "text_color",
    "highlight_color",
    "stroke_width",
    "shadow_enabled",
)

_INTRO_TEXT = "this changed everything"
_SIZE_PX = 96
_REVEAL_S = 3.0
_DURATION_S = 10.0
_GENERATIVE_SETS = style_set_ids(applies_to="generative")


def _look(burn_dicts: list[dict]) -> list[dict]:
    return [{k: bd.get(k) for k in _LOOK_KEYS} for bd in burn_dicts]


def _render(
    style_set_id: str | None, *, word_roles: list[str] | None = None, **resolver_kwargs
) -> tuple[list[dict], dict]:
    """Resolve + burn the way a render does; return (burn_dicts, persisted fields)."""
    agent_text = types.SimpleNamespace(
        text=_INTRO_TEXT if word_roles is None else "three days in PARIS with",
        highlight_word=None,
        word_roles=word_roles,
    )
    agent_form = {"effect": "karaoke-line", "layout": "cluster" if word_roles else "linear"}
    params, intro_px, _source = gb._resolve_intro_overlay_params(
        agent_text, agent_form, style_set_id, size_override_px=_SIZE_PX, **resolver_kwargs
    )
    params.pop("_bs_pregate", None)
    burn_dicts = build_persistent_intro_overlays(
        reveal_window_s=_REVEAL_S, beats=[], start_s=0.0, end_s=_DURATION_S, **params
    )
    persisted = {
        "text_mode": "agent_text",
        "intro_text": agent_text.text,
        "intro_layout": params["layout"],
        "intro_mode": params["layout"],
        "intro_word_roles": params.get("word_roles"),
        "intro_text_size_px": intro_px,
        "intro_placement": gb._intro_placement_from_params(params),
        "style_set_id": style_set_id,
        "duration_s": _DURATION_S,
        # Written by the render exactly as passed (see `base` in
        # `_render_generative_variant`): overrides + knobs persist verbatim.
        "intro_font_family": resolver_kwargs.get("font_family_override"),
        "intro_effect": resolver_kwargs.get("effect_override"),
        "intro_text_color": resolver_kwargs.get("text_color_override"),
        "user_style_knobs": resolver_kwargs.get("user_style_knobs"),
    }
    return burn_dicts, persisted


def _projected_burn_dicts(variant: dict) -> list[dict]:
    elements = text_elements_for_variant(variant)
    assert elements, "adapter projected no element"
    return [bd for elem in elements for bd in elem.source_params["burn_dicts"]]


def _frame(overlay: dict, t: float) -> np.ndarray:
    image = tos._draw_frame(overlay, t - overlay["start_s"], overlay["end_s"] - overlay["start_s"])
    return np.asarray(Image.open(io.BytesIO(bytes(image.encodeToData()))).convert("RGBA"))


@pytest.mark.parametrize("style_set_id", _GENERATIVE_SETS)
def test_every_generative_style_set_projects_the_burned_look(style_set_id):
    burned, variant = _render(style_set_id)

    assert _look(_projected_burn_dicts(variant)) == _look(burned)
    (element,) = text_elements_for_variant(variant)
    assert element.font_family == resolve_overlay_style(style_set_id, "intro")["font_family"]


@pytest.mark.parametrize("style_set_id", _GENERATIVE_SETS)
def test_saved_intro_settles_pixel_identical_to_the_render(style_set_id):
    """A text Save burns the projected element; its settled hold must match.

    (The entrance animation of pop-in/typewriter/bounce/slide-in sets is a
    separate gap: `_BURN_EFFECT_TO_TEXT_ELEMENT` maps them to "static".)
    """
    burned, variant = _render(style_set_id)
    saved = build_overlays_from_text_elements(
        text_elements_for_variant(variant), video_duration_s=_DURATION_S
    )

    t = _DURATION_S - 1.0
    (rendered_hold,) = [bd for bd in burned if bd["start_s"] <= t < bd["end_s"]]
    (saved_hold,) = [bd for bd in saved if bd["start_s"] <= t < bd["end_s"]]
    assert np.array_equal(_frame(rendered_hold, t), _frame(saved_hold, t))


def test_creator_overrides_win_over_the_style_set():
    burned, variant = _render(
        "high_fashion",
        font_family_override="Anton",
        effect_override="slide-up",
        text_color_override="#FF3366",
    )

    assert {bd["font_family"] for bd in burned} == {"Anton"}, "render sanity check"
    assert _look(_projected_burn_dicts(variant)) == _look(burned)


def test_user_style_knobs_win_over_the_style_set():
    knobs = {
        "font_family": "Montserrat",
        "text_color": "#22CC88",
        "highlight_color": "#3344FF",
        "stroke_width": 2,
    }
    burned, variant = _render("candy_pop", user_style_knobs=knobs)

    assert {bd["stroke_width"] for bd in burned} == {2}, "render sanity check"
    assert _look(_projected_burn_dicts(variant)) == _look(burned)


@pytest.mark.parametrize("style_set_id", ["default", "high_fashion", "candy_pop"])
def test_legacy_cluster_projects_the_style_set_face_pairing(style_set_id):
    """The LEGACY cluster profile pairs faces off the resolved intro face."""
    burned, variant = _render(style_set_id, word_roles=["body", "body", "hero", "body", "accent"])

    assert variant["intro_layout"] == "cluster", "render sanity check"
    projected = _projected_burn_dicts(variant)
    assert sorted(map(str, _look(projected))) == sorted(map(str, _look(burned)))


def test_variant_without_style_set_keeps_the_default_face_and_effect():
    burned, variant = _render(None)

    (element,) = text_elements_for_variant(variant)
    assert element.font_family is None
    assert element.effect == "karaoke-line"
    assert _look(_projected_burn_dicts(variant)) == _look(burned)
