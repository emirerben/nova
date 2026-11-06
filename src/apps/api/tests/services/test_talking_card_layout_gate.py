"""Qendresa item 15 glue: the gate, the take-to-source window map, the editor round trip.

Ways this goes wrong, and the check that catches each:
- G1 the layout opens with the flag on but the token off, or the build too old, or no stamp
  (older editors pull every card centre up to 0.62, onto the captions): truth table below.
- G2 `cardLayout` alone opens it without the flag: truth table.
- W1 a title window on the CUT take is handed to grounding unmapped, so it avoids the wrong
  seconds of the source: mapped through the kept segments (cuts before / inside the window).
- W2 a window beyond the kept span is kept (covers nothing) or crashes: dropped / clamped.
- W3 the closing-text window is double-handled: it is only in `occupied` (no helper here).
- E1 a Save drops `caption_clear` and the editor pulls the card back to 0.62.
- E2 a client sets `caption_clear` on a card the server never marked: ignored.
- E3 an unmarked card's section gains a key (byte-identity broken).
- E4 a recipe-derived lane forgets a below-0.62 pip card was laid out against the captions.
- E5 a landscape recipe derives the mark (its geometry is not the portrait band).

KRI-1238 (her Q1 run, job e7d1e06f: a 720x1280 take, `landscape_fit="fit"`, small corner cards):
- F1 a portrait take with the content-plan default "fit" stays closed, although the phone draws
  "fit" and "fill" the same for it (no bars, no crop): truth-table rows 720x1280 / 1080x1920.
- F2 a landscape take with "fit" opens: the letterbox moves her face in y. Stays closed.
- F3 a square or 3:4 take with "fit" stays closed: the phone cover-fills it (sides cropped
  only), so y is exact. Opens.
- F4 a source taller than 9:16 opens because it is "fit": still cropped top and bottom. Closed.
- F5 a KRI-547 face-filled close-up opens because it is portrait: still closed.
- F6 the gate and the renderer disagree on which takes get bars (`fit_transform` changes and
  the gate does not follow): parity test over shapes x fits.
- F7 a rotation-flagged file is judged by its pixel size: the worker passes `display_dims`
  (worker test `test_a_letterboxed_landscape_take_keeps_todays_cards`).
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.pipeline.canvas import PORTRAIT
from app.pipeline.phone_recipe_shared import fit_transform
from app.pipeline.phone_subtitled_lanes import PhoneSubtitledLanes
from app.pipeline.phone_subtitled_plan import compile_phone_subtitled_plan
from app.services.client_protocol import CARD_LAYOUT_PROTOCOL
from app.services.phone_rollout import talking_card_layout_planned_for
from app.services.phone_subtitled_editor import (
    lanes_from_editor_sections,
    lanes_from_recipe,
    sections_from_lanes,
)
from app.tasks.generative_build import (
    _card_layout_context,
    _card_layout_framing_ok,
    _take_window_to_source,
)
from tests.pipeline.test_phone_subtitled_plan import (
    _binding,
    _overlay_card,
    _photo_visual,
)


@pytest.mark.parametrize(
    ("flag", "verified", "protocol", "expected"),
    [
        (True, ["cardLayout"], CARD_LAYOUT_PROTOCOL, True),
        (True, ["cardLayout"], CARD_LAYOUT_PROTOCOL + 5, True),
        (False, ["cardLayout"], CARD_LAYOUT_PROTOCOL, False),
        (True, [], CARD_LAYOUT_PROTOCOL, False),
        (True, ["authoredText"], CARD_LAYOUT_PROTOCOL, False),
        (True, ["cardLayout"], CARD_LAYOUT_PROTOCOL - 1, False),
        (True, ["cardLayout"], None, False),
    ],
)
def test_gate_truth_table(monkeypatch, flag, verified, protocol, expected):
    monkeypatch.setattr(settings, "talking_card_layout_enabled", flag)
    monkeypatch.setattr(settings, "phone_render_verified_features", verified)
    assert talking_card_layout_planned_for(protocol) is expected


def test_gate_defaults_closed():
    assert settings.talking_card_layout_enabled is False
    assert CARD_LAYOUT_PROTOCOL == 40


def test_window_map_without_cuts_is_identity():
    assert _take_window_to_source(2.0, 5.0, [(0.0, 20.0)]) == (2.0, 5.0)


def test_window_map_after_a_cut_shifts_to_source_time():
    # source 0-4 and 6-20 kept: take time 5 is source 7.
    assert _take_window_to_source(5.0, 8.0, [(0.0, 4.0), (6.0, 20.0)]) == (7.0, 10.0)


def test_window_map_inside_a_cut_covers_the_removed_span():
    # take 3-6 spans the boundary at take 4 (source 4 -> 6).
    assert _take_window_to_source(3.0, 6.0, [(0.0, 4.0), (6.0, 20.0)]) == (3.0, 8.0)


def test_window_starting_at_a_cut_boundary_starts_after_the_cut():
    assert _take_window_to_source(4.0, 6.0, [(0.0, 4.0), (6.0, 20.0)]) == (6.0, 8.0)


def test_window_beyond_the_kept_span_is_dropped_or_clamped():
    keep = [(0.0, 4.0), (6.0, 10.0)]  # 8 s of take
    assert _take_window_to_source(8.0, 12.0, keep) is None
    assert _take_window_to_source(7.0, 12.0, keep) == (9.0, 10.0)
    assert _take_window_to_source(1.0, 1.0, keep) is None
    assert _take_window_to_source(1.0, 2.0, []) is None


def test_context_band_is_measured_and_windows_are_mapped():
    policy = {
        "_caption_boxes": [
            {"start_s": 0, "end_s": 1, "box": (0.1, 0.70, 0.9, 0.76), "kind": "caption"},
            {"start_s": 1, "end_s": 2, "box": (0.1, 0.66, 0.9, 0.80), "kind": "caption"},
        ]
    }
    ctx = _card_layout_context(
        open_=True,
        policy=policy,
        text_rows=[{"start_s": 5.0, "end_s": 8.0}, {"start_s": 99.0, "end_s": 100.0}],
        keep_segments=[(0.0, 4.0), (6.0, 20.0)],
    )
    assert ctx.caption_band == (0.66, 0.80)
    assert ctx.text_windows == ((7.0, 10.0),)


def test_context_is_none_when_closed_and_band_none_without_boxes():
    assert _card_layout_context(open_=False, policy=None, text_rows=[], keep_segments=[]) is None
    ctx = _card_layout_context(open_=True, policy=None, text_rows=[], keep_segments=[])
    assert ctx.caption_band is None and ctx.text_windows == ()


def test_marked_card_projects_and_unmarked_card_is_byte_identical():
    plain = _overlay_card(id="a")
    marked = _overlay_card(id="b", caption_clear=True)
    sections = sections_from_lanes(PhoneSubtitledLanes(overlays=[plain, marked]), labels={})
    a, b = sections["media_overlays"]
    assert "caption_clear" not in a
    assert b["caption_clear"] is True


def test_save_keeps_the_servers_mark_and_ignores_the_clients():
    photo = _photo_visual()
    marked = _overlay_card(id="b", caption_clear=True, y_frac=0.8)
    plain = _overlay_card(id="a")
    previous = PhoneSubtitledLanes(overlays=[marked, plain])
    sections = sections_from_lanes(previous, labels={})["media_overlays"]
    moved = [{**sections[0], "y_frac": 0.85}, {**sections[1], "caption_clear": True}]
    out = lanes_from_editor_sections(
        previous=previous, sound_effects=None, media_overlays=moved, visuals=(photo,)
    )
    by_id = {c.id: c for c in out.overlays}
    assert by_id["b"].caption_clear is True and by_id["b"].y_frac == pytest.approx(0.85)
    assert by_id["a"].caption_clear is False
    # a card the previous lanes never held cannot claim it
    added = [{**sections[0], "id": "new", "caption_clear": True}]
    out2 = lanes_from_editor_sections(
        previous=previous, sound_effects=None, media_overlays=added, visuals=(photo,)
    )
    assert out2.overlays[0].caption_clear is False


def _recipe_with(card):
    photo = _photo_visual()
    return photo, compile_phone_subtitled_plan(
        (_binding(duration_s=10.0),),
        caption_cues=[],
        visuals=(photo,),
        lanes=PhoneSubtitledLanes(overlays=[card]),
    )


def test_recipe_derivation_marks_a_pip_card_below_the_caption_band():
    photo, recipe = _recipe_with(_overlay_card(id="low", caption_clear=True, y_frac=0.85))
    derived = lanes_from_recipe(recipe, visuals=(photo,))
    assert derived.overlays[0].caption_clear is True


def test_recipe_derivation_leaves_a_normal_card_unmarked():
    photo, recipe = _recipe_with(_overlay_card(id="top", y_frac=0.3))
    derived = lanes_from_recipe(recipe, visuals=(photo,))
    assert derived.overlays[0].caption_clear is False


@pytest.mark.parametrize(
    ("face_filled", "fit", "w", "h", "expected"),
    [
        (False, "fill", 1080, 1920, True),
        (False, "fill", 1920, 1080, True),
        (False, "fill", 1080, 1930, True),  # within the 0.01 tolerance of 9:16
        (True, "fill", 1920, 1080, False),  # KRI-547 face fill: close-up crop
        (False, "fit", 1920, 1080, False),  # letterboxed take moves the face in y
        (False, "fill", 1080, 2340, False),  # taller than portrait: cropped top and bottom
        (False, "fill", 100, 0, False),
        # KRI-1238: "fit" only letterboxes a landscape take; a portrait one is cover-filled.
        (False, "fit", 720, 1280, True),  # F1: her Q1 take with the content-plan default
        (False, "fit", 1080, 1920, True),  # F1
        (False, "fit", 1080, 1930, True),  # F1, within the 9:16 tolerance
        (False, "fit", 1080, 1080, True),  # F3: square, sides cropped only
        (False, "fit", 1080, 1440, True),  # F3: 3:4 portrait, sides cropped only
        (False, "fit", 1281, 1280, False),  # F2: a hair wider than tall is letterboxed
        (False, "fit", 1080, 2340, False),  # F4: taller than 9:16 is still cropped in y
        (True, "fit", 720, 1280, False),  # F5: a face-filled close-up stays closed
        (False, "fit", 100, 0, False),
    ],
)
def test_card_layout_framing_truth_table(face_filled, fit, w, h, expected):
    assert (
        _card_layout_framing_ok(
            face_filled=face_filled, landscape_fit=fit, display_width=w, display_height=h
        )
        is expected
    )


@pytest.mark.parametrize("fit", ["fit", "fill"])
@pytest.mark.parametrize(
    ("w", "h"),
    [(720, 1280), (1080, 1920), (1080, 1440), (1080, 1080), (1281, 1280), (1920, 1080)],
)
def test_card_layout_framing_follows_the_phone_letterbox(fit, w, h):
    # F6: the gate closes for a speaker the phone letterboxes, and only for one -- the same
    # rule the phone compiler draws with (`fit_transform`; iOS `fitTransform` mirrors it).
    # Shapes within 9:16 (or wider) only, so the taller-than-9:16 crop rule stays out of it.
    letterboxed = fit_transform(w, h, PORTRAIT, fit).scale != 1.0
    opened = _card_layout_framing_ok(
        face_filled=False, landscape_fit=fit, display_width=w, display_height=h
    )
    assert opened is not letterboxed
