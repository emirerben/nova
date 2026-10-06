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
Placement parity lives in test_intro_placement_parity.py; the left-anchored
intros' vertical anchoring (block center in the editor, top on the burn) is
pinned at the bottom of this file.
"""

from __future__ import annotations

import io
import types

import numpy as np
import pytest
from PIL import Image

import app.tasks.generative_build as gb
from app.agents._schemas.text_element import (
    TOP_ANCHORED_BURN_PARAM,
    TextElement,
    text_elements_for_variant,
)
from app.pipeline import text_overlay_skia as tos
from app.pipeline.canvas import PORTRAIT, Canvas, canvas_for_orientation
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
# Sets that pin x only with text_anchor=left; see `_as_drawn`. The renderer
# top-anchors these intros (`_resolve_vertical_anchor`).
_LEFT_ANCHORED_SETS = ("word_reveal", "typewriter", "ai_answer")


def _look(burn_dicts: list[dict]) -> list[dict]:
    return [{k: bd.get(k) for k in _LOOK_KEYS} for bd in burn_dicts]


def _as_drawn(burn_dicts: list[dict]) -> list[dict]:
    """Burn dicts in time order, with the renderer's y fallback spelled out.

    The half-pinned sets burn `position_x_frac` with no y, and `_resolve_anchor`
    falls back to the named position's y. The adapter projects the block's
    center as an explicit y and the compiler turns it back into that top y
    (test_half_pinned_style_sets_project_the_burned_block), so the saved dict
    spells out the y the render left implicit.
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
    style_set_id: str | None,
    *,
    word_roles: list[str] | None = None,
    orientation: str | None = None,
    **resolver_kwargs,
) -> tuple[list[dict], dict]:
    """Resolve + burn the way a render does; return (burn_dicts, persisted fields)."""
    agent_text = types.SimpleNamespace(
        text=_INTRO_TEXT if word_roles is None else "three days in PARIS with",
        highlight_word=None,
        word_roles=word_roles,
    )
    agent_form = {"effect": "karaoke-line", "layout": "cluster" if word_roles else "linear"}
    canvas = canvas_for_orientation(orientation)
    params, intro_px, _source = gb._resolve_intro_overlay_params(
        agent_text,
        agent_form,
        style_set_id,
        size_override_px=_SIZE_PX,
        canvas=canvas,
        **resolver_kwargs,
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
        "orientation": orientation,
    }
    return burn_dicts, persisted


def _projected_burn_dicts(variant: dict) -> list[dict]:
    elements = text_elements_for_variant(variant)
    assert elements, "adapter projected no element"
    return [bd for elem in elements for bd in elem.source_params["burn_dicts"]]


def _frame(overlay: dict, t: float, canvas: Canvas = PORTRAIT) -> np.ndarray:
    image = tos._draw_frame(
        overlay,
        t - overlay["start_s"],
        overlay["end_s"] - overlay["start_s"],
        render_canvas=canvas,
    )
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


def _text_save(variant: dict, elements: list[TextElement]) -> list[dict]:
    """Burn dicts the authoritative text Save reburns for `elements`."""
    return gb._text_element_burn_dicts(
        {
            **variant,
            "text_elements": [element.model_dump() for element in elements],
            "text_elements_user_edited": True,
        }
    )


@pytest.mark.parametrize("style_set_id", _GENERATIVE_SETS)
def test_text_save_reburns_the_rendered_intro(style_set_id):
    """The real Save path: persisted elements → `_text_element_burn_dicts`.

    Covers what the compiler alone does not: the typewriter `reveal_schedule_s`
    re-attachment (the intro projection carries no schedule, so none appears)
    and the authoritative-save layout contract, under which the left-anchored
    sets used to rise by half their block.
    """
    burned, variant = _render(style_set_id)
    saved = _text_save(variant, text_elements_for_variant(variant))

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


# ── Left-anchored intros: block center in the editor, top anchoring on the burn ──
#
# `_resolve_vertical_anchor` top-anchors a left-anchored burn with no
# `vertical_anchor` at its y. A TextElement's y is the block CENTER (CSS editor,
# `vertical_anchor=center` on every authoritative Save), so the adapter projects
# the burned block's center and marks the element; the compiler turns that center
# back into the burned top y, which also keeps the render's pop-in pivot.

# Every way an intro burns left-anchored with no vertical_anchor: the curated
# sets, and a creator knob on a set that leaves x/y unpinned (any named position).
_LEFT_INTRO_CASES = [
    *[pytest.param(set_id, {}, id=set_id) for set_id in _LEFT_ANCHORED_SETS],
    pytest.param("default", {"user_style_knobs": {"text_anchor": "left"}}, id="knob-left-center"),
    pytest.param(
        "default",
        {"user_style_knobs": {"text_anchor": "left", "position": "top"}},
        id="knob-left-top",
    ),
    pytest.param(
        "default",
        {"user_style_knobs": {"text_anchor": "left"}, "effect_override": "pop-in"},
        id="knob-left-pop-in",
    ),
    pytest.param("word_reveal", {"orientation": "landscape"}, id="word_reveal-landscape"),
]
_FRAME_TIMES = (0.05, 0.2, _DURATION_S - 1.0)


def _without_marker(element: TextElement) -> TextElement:
    params = {k: v for k, v in element.source_params.items() if k != TOP_ANCHORED_BURN_PARAM}
    return element.model_copy(update={"source_params": params})


@pytest.mark.parametrize(("style_set_id", "resolver_kwargs"), _LEFT_INTRO_CASES)
def test_left_intro_projects_its_block_center_and_saves_where_it_burned(
    style_set_id, resolver_kwargs
):
    burned, variant = _render(style_set_id, **resolver_kwargs)
    assert {(bd["text_anchor"], bd.get("vertical_anchor")) for bd in burned} == {("left", None)}, (
        "render sanity: left-anchored, top-anchored by default"
    )
    (element,) = text_elements_for_variant(variant)
    assert element.position == "custom" and element.alignment == "left"
    assert element.source_params[TOP_ANCHORED_BURN_PARAM] is True
    burned_top = burned[-1].get("position_y_frac") or _POSITION_Y[burned[-1]["position"]]
    assert element.y_frac > burned_top, "the editor y must be the block center, not its top"

    saved = _text_save(variant, [element])

    # The reburn keeps the burn's own top anchoring, frac for frac...
    assert all("vertical_anchor" not in bd for bd in saved)
    assert {bd["position_y_frac"] for bd in saved} == {burned_top}
    # ...so every frame matches, entrance included (pop-in scales about the anchor).
    canvas = canvas_for_orientation(variant["orientation"])
    for t in _FRAME_TIMES:
        assert np.array_equal(
            _frame(_at(burned, t), t, canvas), _frame(_at(saved, t), t, canvas)
        ), t


@pytest.mark.parametrize(("style_set_id", "resolver_kwargs"), _LEFT_INTRO_CASES)
def test_projected_center_is_the_burned_block_center(style_set_id, resolver_kwargs):
    """Independent of the marker: the plain center contract (what the CSS editor
    previews, and what a Save without the marker burns) settles on the rendered
    block pixel for pixel. Pins the measurement against the renderer's own
    centering of the drawn block."""
    burned, variant = _render(style_set_id, **resolver_kwargs)
    (element,) = text_elements_for_variant(variant)

    saved = _text_save(variant, [_without_marker(element)])

    assert {bd.get("vertical_anchor") for bd in saved} == {"center"}
    canvas = canvas_for_orientation(variant["orientation"])
    t = _DURATION_S - 1.0
    assert np.array_equal(_frame(_at(burned, t), t, canvas), _frame(_at(saved, t), t, canvas))


def test_edited_left_intro_stays_centered_on_the_editor_y():
    """After a text edit the block re-measures: it stays centered where the editor
    draws it (same settled pixels as the center contract), and only the entrance
    pivot follows the burn's top anchoring."""
    _burned, variant = _render("word_reveal")
    (element,) = text_elements_for_variant(variant)
    edited = element.model_copy(
        update={"text": "this changed everything about how I pack for a long weekend"}
    )

    saved = _text_save(variant, [edited])
    centered = _text_save(variant, [_without_marker(edited)])

    t = _DURATION_S - 1.0
    assert np.array_equal(_frame(_at(saved, t), t), _frame(_at(centered, t), t))
    assert saved[-1]["position_y_frac"] < _POSITION_Y["center"], "a taller block starts higher"


@pytest.mark.parametrize("alignment", ["center", "right"])
def test_realigned_left_intro_compiles_to_the_center_contract(alignment):
    """Once the creator re-aligns it, the element is an ordinary centered box."""
    _burned, variant = _render("typewriter")
    (element,) = text_elements_for_variant(variant)

    saved = _text_save(variant, [element.model_copy(update={"alignment": alignment})])

    assert {(bd["vertical_anchor"], bd["position_y_frac"]) for bd in saved} == {
        ("center", element.y_frac)
    }


@pytest.mark.parametrize("style_set_id", _GENERATIVE_SETS)
def test_only_left_anchored_intros_are_marked(style_set_id):
    """Centered sets project and compile exactly as before."""
    _burned, variant = _render(style_set_id)

    (element,) = text_elements_for_variant(variant)
    marked = TOP_ANCHORED_BURN_PARAM in element.source_params
    assert marked == (style_set_id in _LEFT_ANCHORED_SETS)


def test_unmeasurable_block_falls_back_open(monkeypatch):
    """A measurement failure never breaks a read or a Save: the adapter keeps the
    burned y unmarked, and a marked element keeps the center contract."""
    _burned, variant = _render("ai_answer")
    (marked,) = text_elements_for_variant(variant)

    def boom(*_args, **_kwargs):
        raise RuntimeError("no skia")

    monkeypatch.setattr(tos, "static_block_height_px", boom)
    (element,) = text_elements_for_variant(variant)
    assert element.y_frac == _POSITION_Y["center"]
    assert TOP_ANCHORED_BURN_PARAM not in element.source_params

    saved = _text_save(variant, [marked])
    assert {(bd["vertical_anchor"], bd["position_y_frac"]) for bd in saved} == {
        ("center", marked.y_frac)
    }
