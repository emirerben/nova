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
RENDER produced, and the saved element reburns the render's [reveal, hold]
pair — entrance effect included — so its frames burn pixel-identically.
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
from app.pipeline.text_overlay import _POSITION_Y

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

# Intro entrances the read adapter used to flatten to `static` (nine curated sets
# use one). slide-in draws settled and dissolve-out is applied to the encoded
# frame sequence, so only the rest move in a single `_draw_frame`.
_ENTRANCE_EFFECTS = (
    "pop-in",
    "typewriter",
    "stream-in",
    "bounce",
    "slide-in",
    "scale-up",
    "dissolve-out",
)
_FRAME_DRAWN_ENTRANCES = frozenset({"pop-in", "typewriter", "stream-in", "bounce", "scale-up"})
# Effect-specific keys other paths attach (lyric pop-in suffix, smart-edit
# typewriter schedule, v2 motion). The intro builder emits none of them, so a
# reburn that grew one would no longer be the render.
_EFFECT_SPECIFIC_KEYS = frozenset({"pop_animated_suffix", "reveal_schedule_s", "motion"})
# Sets that pin x only with text_anchor=left; see `_as_drawn`.
_HALF_PINNED_LEFT_SETS = frozenset({"word_reveal", "typewriter", "ai_answer"})


def _look(burn_dicts: list[dict]) -> list[dict]:
    return [{k: bd.get(k) for k in _LOOK_KEYS} for bd in burn_dicts]


def _as_drawn(burn_dicts: list[dict]) -> list[dict]:
    """Burn dicts in time order, with the renderer's y fallback spelled out.

    The half-pinned sets burn `position_x_frac` with no y, and `_resolve_anchor`
    falls back to the named position's y. The adapter projects that same y
    explicitly (test_half_pinned_style_sets_project_at_the_burned_y), so the
    saved dict spells out the y the render left implicit.
    """
    drawn = []
    for bd in burn_dicts:
        if bd.get("position_x_frac") is not None and bd.get("position_y_frac") is None:
            bd = {**bd, "position_y_frac": _POSITION_Y[bd["position"]]}
        drawn.append(bd)
    return sorted(drawn, key=lambda bd: bd["start_s"])


def _at(burn_dicts: list[dict], t: float) -> dict:
    (overlay,) = [bd for bd in burn_dicts if bd["start_s"] <= t < bd["end_s"]]
    return overlay


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
def test_saved_intro_reburns_the_rendered_burn_dicts(style_set_id):
    """A text Save burns the projected element: it must compile back to the
    render's [reveal, hold] pair key for key, the reveal's entrance included.
    The read adapter used to flatten pop-in/typewriter/stream-in/bounce/slide-in
    to one static bar, so the first Save dropped the set's entrance."""
    burned, variant = _render(style_set_id)
    saved = build_overlays_from_text_elements(
        text_elements_for_variant(variant), video_duration_s=_DURATION_S
    )

    assert _as_drawn(saved) == _as_drawn(burned)


@pytest.mark.parametrize("effect", _ENTRANCE_EFFECTS)
def test_entrance_effect_reburns_the_render(effect):
    """Per effect, through the creator's animation override (no set geometry):
    the element carries the effect, the reveal+hold dicts are byte-identical and
    no effect-specific key appears on either side."""
    burned, variant = _render("default", effect_override=effect)
    (element,) = text_elements_for_variant(variant)
    saved = build_overlays_from_text_elements([element], video_duration_s=_DURATION_S)

    assert [bd["effect"] for bd in _as_drawn(burned)] == [effect, "static"], "render sanity"
    assert element.effect == effect
    assert _as_drawn(saved) == _as_drawn(burned)
    assert not _EFFECT_SPECIFIC_KEYS & {key for bd in burned + saved for key in bd}


@pytest.mark.parametrize("effect", sorted(_FRAME_DRAWN_ENTRANCES))
def test_saved_entrance_frames_match_the_render(effect):
    """The entrance itself, in pixels: early reveal frames match the render and
    are still moving (they differ from the settled hold), so a Save no longer
    pops the intro on fully formed."""
    burned, variant = _render("default", effect_override=effect)
    saved = build_overlays_from_text_elements(
        text_elements_for_variant(variant), video_duration_s=_DURATION_S
    )
    settled = _frame(_at(saved, _DURATION_S - 1.0), _DURATION_S - 1.0)

    for t in (0.05, 0.2):
        saved_frame = _frame(_at(saved, t), t)
        assert np.array_equal(_frame(_at(burned, t), t), saved_frame), t
        assert not np.array_equal(saved_frame, settled), f"no entrance at t={t}"


_TEXT_SAVE_CASES = [
    pytest.param(
        style_set_id,
        marks=pytest.mark.xfail(
            strict=True,
            reason=(
                "Pre-existing placement gap: the Save path compiles with "
                "independent_box_alignment=True (vertical_anchor=center), but the render "
                "burned these left-anchored intros top-anchored (`_resolve_vertical_anchor`), "
                "so a Save lifts them by half the block height."
            ),
        ),
    )
    if style_set_id in _HALF_PINNED_LEFT_SETS
    else style_set_id
    for style_set_id in _GENERATIVE_SETS
]


@pytest.mark.parametrize("style_set_id", _TEXT_SAVE_CASES)
def test_text_save_reburns_the_rendered_intro(style_set_id):
    """The real Save path: persisted elements → `_text_element_burn_dicts`.

    Covers what the compiler alone does not: the typewriter `reveal_schedule_s`
    re-attachment (the intro projection carries no schedule, so none appears)
    and the authoritative-save layout contract.
    """
    burned, variant = _render(style_set_id)
    elements = text_elements_for_variant(variant)
    saved = gb._text_element_burn_dicts(
        {
            **variant,
            "text_elements": [element.model_dump() for element in elements],
            "text_elements_user_edited": True,
        }
    )

    assert [bd["effect"] for bd in _as_drawn(saved)] == [bd["effect"] for bd in _as_drawn(burned)]
    assert not any("reveal_schedule_s" in bd for bd in saved)
    for t in (0.05, _DURATION_S - 1.0):
        assert np.array_equal(_frame(_at(burned, t), t), _frame(_at(saved, t), t)), t


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
