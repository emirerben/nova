"""Round-trip: a "Slide down" intro must animate on the render AND after a text Save.

The instant-editor picker offers `slide-down` (`INTRO_ANIMATIONS` /
`_INTRO_ANIMATION_EFFECTS`) and Skia, the CSS preview and iOS all animate it,
but `generative_overlays._SKIA_EFFECTS` lacked it, so `build_intro_overlay`
coerced it to `static`: the first render (`_resolve_intro_overlay_params` →
`build_persistent_intro_overlays`) and every Save of a slide-down element
(`build_overlays_from_text_elements`) burned the intro with no entrance. The
read adapter's `_BURN_EFFECT_TO_TEXT_ELEMENT` flattened it the same way.

`slide-up` rides along as the control: it always round-tripped.
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

_INTRO_TEXT = "this changed everything"
_SIZE_PX = 96
_REVEAL_S = 3.0
_DURATION_S = 10.0
_SLIDE_EFFECTS = ("slide-up", "slide-down")
# The adapter does not yet project the resolved face/stroke/shadow of the
# default set (a separate gap), so the saved block's glyphs differ from the
# render's. This file pins the entrance: everything else must match.
_UNPROJECTED_LOOK_KEYS = frozenset({"font_family", "stroke_width", "shadow_enabled"})


def _render(effect: str) -> tuple[list[dict], dict]:
    """Resolve + burn the way a render does; return (burn_dicts, persisted fields)."""
    agent_text = types.SimpleNamespace(text=_INTRO_TEXT, highlight_word=None, word_roles=None)
    params, intro_px, _source = gb._resolve_intro_overlay_params(
        agent_text,
        {"effect": "karaoke-line", "layout": "linear"},
        "default",
        size_override_px=_SIZE_PX,
        effect_override=effect,
    )
    params.pop("_bs_pregate", None)
    burn_dicts = build_persistent_intro_overlays(
        reveal_window_s=_REVEAL_S, beats=[], start_s=0.0, end_s=_DURATION_S, **params
    )
    persisted = {
        "text_mode": "agent_text",
        "intro_text": _INTRO_TEXT,
        "intro_layout": params["layout"],
        "intro_mode": params["layout"],
        "intro_text_size_px": intro_px,
        "intro_placement": gb._intro_placement_from_params(params),
        "style_set_id": "default",
        "duration_s": _DURATION_S,
        "intro_effect": effect,
    }
    return sorted(burn_dicts, key=lambda bd: bd["start_s"]), persisted


def _saved(variant: dict) -> list[dict]:
    saved = build_overlays_from_text_elements(
        text_elements_for_variant(variant), video_duration_s=_DURATION_S
    )
    return sorted(saved, key=lambda bd: bd["start_s"])


def _at(burn_dicts: list[dict], t: float) -> dict:
    (overlay,) = [bd for bd in burn_dicts if bd["start_s"] <= t < bd["end_s"]]
    return overlay


def _entrance(burn_dicts: list[dict]) -> list[dict]:
    return [{k: v for k, v in bd.items() if k not in _UNPROJECTED_LOOK_KEYS} for bd in burn_dicts]


def _frame(overlay: dict, t: float) -> np.ndarray:
    image = tos._draw_frame(overlay, t - overlay["start_s"], overlay["end_s"] - overlay["start_s"])
    return np.asarray(Image.open(io.BytesIO(bytes(image.encodeToData()))).convert("RGBA"))


def _top_row(frame: np.ndarray) -> int:
    return int(np.flatnonzero(frame[:, :, 3].any(axis=1))[0])


@pytest.mark.parametrize("effect", _SLIDE_EFFECTS)
def test_slide_intro_reburns_the_render(effect):
    burned, variant = _render(effect)
    (element,) = text_elements_for_variant(variant)

    assert [bd["effect"] for bd in burned] == [effect, "static"]
    assert element.effect == effect
    assert _entrance(_saved(variant)) == _entrance(burned)


@pytest.mark.parametrize("effect", _SLIDE_EFFECTS)
def test_saved_slide_intro_replays_the_rendered_entrance(effect):
    """Early reveal frames are still moving (they differ from the settled hold)
    and sit as far from it as the render's do, so a Save neither pops the intro
    on fully formed nor changes how far it travels."""
    burned, variant = _render(effect)
    saved = _saved(variant)
    t_settled = _DURATION_S - 1.0
    rendered_settled = _frame(_at(burned, t_settled), t_settled)
    saved_settled = _frame(_at(saved, t_settled), t_settled)

    for t in (0.05, 0.2):
        rendered = _frame(_at(burned, t), t)
        saved_frame = _frame(_at(saved, t), t)
        assert not np.array_equal(saved_frame, saved_settled), f"no entrance at t={t}"
        travel = _top_row(rendered) - _top_row(rendered_settled)
        assert travel != 0, f"render sanity at t={t}"
        assert _top_row(saved_frame) - _top_row(saved_settled) == travel, t
