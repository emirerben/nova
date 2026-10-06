"""The editor's resolved storyboard look burns exactly like the authored presets.

`resolve_narrated_storyboard_look` hands the editor a storyboard bar with its
cloud look spelled out (custom y, px size, face) so the iOS and web previews
draw it where the cloud burns it. A Save persists that resolved bar and every
later reburn burns it, so it must produce the same pixels as the preset bar
the storyboard authored.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from app.agents._schemas.text_element import (
    TextElement,
    is_narrated_storyboard_element,
    resolve_narrated_storyboard_look,
)
from app.pipeline import text_overlay_skia as tos
from app.pipeline.generative_overlays import build_overlays_from_text_elements
from app.pipeline.transcribe import Transcript, Word
from app.tasks import generative_build as gb


def _storyboard_elements() -> list[TextElement]:
    transcript = Transcript(
        words=[
            Word("Intro", 0.0, 0.4, 1.0),
            Word("score", 1.0, 1.2, 1.0),
            Word("six", 1.2, 1.5, 1.0),
            Word("four", 1.5, 1.8, 1.0),
        ],
        language="en",
    )
    rows = gb._narrated_storyboard_text_elements(
        transcript=transcript,
        step_timings=[SimpleNamespace(step_id="step_0", start_s=0.0, end_s=2.0)],
        clip_assignments=[SimpleNamespace(step_id="step_0", clip_path="/tmp/a.mp4")],
        clip_id_by_path={"/tmp/a.mp4": "clip_0"},
        creator_request="Add intro text, player names, and scores",
        explicit_opening_title="Match Day Final",
        storyboard={"overlays": []},
    )
    return [TextElement.model_validate(row) for row in rows]


def _burn(element: TextElement) -> dict:
    # The same compile `_text_element_burn_dicts` runs on every reburn.
    [overlay] = build_overlays_from_text_elements(
        [element], video_duration_s=10.0, independent_box_alignment=True
    )
    return overlay


def test_storyboard_bars_resolve_to_the_cloud_look() -> None:
    by_text = {elem.text: resolve_narrated_storyboard_look(elem) for elem in _storyboard_elements()}

    assert set(by_text) == {"Match Day Final", "PLAYER 1", "six four"}
    title = by_text["Match Day Final"]
    assert (title.position, title.x_frac, title.y_frac) == ("custom", 0.5, 0.15)
    assert (title.size_px, title.font_family) == (120.0, "Playfair Display")
    player = by_text["PLAYER 1"]
    assert (player.position, player.y_frac, player.size_px) == ("custom", 0.85, 36.0)
    assert player.font_family == "Playfair Display"
    score = by_text["six four"]
    assert (score.y_frac, score.size_px, score.effect) == (0.15, 120.0, "pop-in")
    # Identity, timing and provenance are untouched.
    for elem in _storyboard_elements():
        resolved = resolve_narrated_storyboard_look(elem)
        assert is_narrated_storyboard_element(resolved)
        assert (resolved.id, resolved.start_s, resolved.end_s, resolved.source_params) == (
            elem.id,
            elem.start_s,
            elem.end_s,
            elem.source_params,
        )


def test_resolving_is_idempotent_and_keeps_creator_choices() -> None:
    title = _storyboard_elements()[0]
    resolved = resolve_narrated_storyboard_look(title)
    assert resolve_narrated_storyboard_look(resolved) == resolved

    moved = title.model_copy(
        update={"position": "custom", "x_frac": 0.3, "y_frac": 0.6, "font_family": "Inter"}
    )
    kept = resolve_narrated_storyboard_look(moved)
    assert (kept.x_frac, kept.y_frac, kept.font_family) == (0.3, 0.6, "Inter")
    assert kept.size_px == 120.0


def _assert_same_pixels(authored: TextElement, label: object) -> None:
    resolved = resolve_narrated_storyboard_look(authored)
    authored_burn, resolved_burn = _burn(authored), _burn(resolved)
    duration = authored.end_s - authored.start_s
    drew = False
    for t_local in (0.0, duration * 0.25, duration * 0.5, duration - 0.01):
        authored_px = tos._skia_image_to_rgba_array(
            tos._draw_frame(authored_burn, t_local, duration)
        )
        resolved_px = tos._skia_image_to_rgba_array(
            tos._draw_frame(resolved_burn, t_local, duration)
        )
        drew = drew or bool(authored_px.any())
        assert np.array_equal(authored_px, resolved_px), (authored.text, label, t_local)
    assert drew, (authored.text, label)


@pytest.mark.parametrize("position", ["top", "middle", "bottom"])
@pytest.mark.parametrize("size_class", ["small", "large", None])
def test_storyboard_resolved_look_burns_identically(position, size_class) -> None:
    for elem in _storyboard_elements():
        authored = elem.model_copy(update={"position": position, "size_class": size_class})
        _assert_same_pixels(authored, (position, size_class))


def test_ios_saved_storyboard_row_burns_identically() -> None:
    """An iOS Save wrote y 0.5 + Fraunces onto named-position bars. The burn
    ignored that y_frac; the resolved bar must burn exactly the same."""
    for elem in _storyboard_elements():
        saved = elem.model_copy(update={"x_frac": 0.5, "y_frac": 0.5, "font_family": "Fraunces"})
        resolved = resolve_narrated_storyboard_look(saved)
        assert resolved.y_frac == {"top": 0.15, "bottom": 0.85}[elem.position]
        assert resolved.font_family == "Fraunces"
        _assert_same_pixels(saved, "ios-saved")
