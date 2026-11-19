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
from app.services.phone_card_layout import CardLayoutContext
from app.services.phone_overlay_grounding import talking_card_strip_planned_for, with_card_strip
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


# KRI-1478: the strip (a cropped full-width card) opens only on top of the card layout, with its
# own switch, and only for a build the phone verified to draw a cropped card. Ways it goes wrong:
# the strip opens with the layout closed; with `sourceCrop` or `mediaCards` unverified (the
# recipe would be refused at validation, losing every card); with no stamped build.
_STRIP_TOKENS = ["cardLayout", "sourceCrop", "mediaCards"]


@pytest.mark.parametrize(
    ("strip", "layout", "verified", "protocol", "expected"),
    [
        (True, True, _STRIP_TOKENS, CARD_LAYOUT_PROTOCOL, True),
        (False, True, _STRIP_TOKENS, CARD_LAYOUT_PROTOCOL, False),
        (True, False, _STRIP_TOKENS, CARD_LAYOUT_PROTOCOL, False),
        (True, True, ["cardLayout", "mediaCards"], CARD_LAYOUT_PROTOCOL, False),
        (True, True, ["cardLayout", "sourceCrop"], CARD_LAYOUT_PROTOCOL, False),
        (True, True, ["sourceCrop", "mediaCards"], CARD_LAYOUT_PROTOCOL, False),
        (True, True, _STRIP_TOKENS, CARD_LAYOUT_PROTOCOL - 1, False),
        (True, True, _STRIP_TOKENS, None, False),
    ],
)
def test_strip_gate_truth_table(monkeypatch, strip, layout, verified, protocol, expected):
    monkeypatch.setattr(settings, "talking_card_strip_enabled", strip)
    monkeypatch.setattr(settings, "talking_card_layout_enabled", layout)
    monkeypatch.setattr(settings, "phone_render_verified_features", verified)
    assert talking_card_strip_planned_for(protocol) is expected
    context = CardLayoutContext(caption_band=(0.62, 0.72))
    assert with_card_strip(context, protocol).strip is expected
    assert with_card_strip(None, protocol) is None, "a closed layout stays closed"


def test_strip_gate_defaults_closed_and_leaves_the_context_alone():
    assert settings.talking_card_strip_enabled is False
    context = CardLayoutContext(caption_band=(0.62, 0.72))
    assert with_card_strip(context, CARD_LAYOUT_PROTOCOL) is context
    assert context.strip is False


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


# --- KRI-1478 worker: the strip through the real phone Talking writer ----------------------
#
# The item-15 worker job (15 Visuals, the real `_run_phone_subtitled_job`), run with the strip
# off and on. Ways the worker glue goes wrong: the strip never reaches grounding (the context
# is built without it); the bind never measures the text column, so every card silently stays
# whole; the bind measures although no card has a strip; a strip sits in another band than the
# card it replaces (so it could reach her face or captions, which the item-15 tests only prove
# clear for that band).


def _strip_worker_job(monkeypatch, *, strip: bool):
    import app.services.phone_overlay_grounding as ogm
    import app.services.phone_visuals as phone_visuals_mod
    import app.services.pipeline_trace as pipeline_trace
    from app.services.background_text_focus import TextColumn
    from tests.tasks import test_qendresa_card_layout_worker as worker
    from tests.tasks.test_kri841_talking_ending_clip import _recipe

    monkeypatch.setattr(ogm.settings, "gemini_api_key", "", raising=False)
    monkeypatch.setattr(pipeline_trace, "record_pipeline_event", lambda *a, **k: None)
    job = worker._cards_job(monkeypatch)
    monkeypatch.setattr(settings, "talking_card_strip_enabled", strip)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [*settings.phone_render_verified_features, "sourceCrop", "mediaCards"],
    )
    measured: list[bool] = []

    def bind(_open, *, job_id, pins, measure_text=False):  # noqa: ARG001
        measured.append(measure_text)
        visuals = []
        for media_id in pins:
            aspect = worker.SPEC[media_id][2]
            column = (
                TextColumn(
                    left=0.2,
                    right=0.62,
                    line_pitch=0.034,
                    source_width=1920,
                    source_height=round(1920 / aspect),
                )
                if measure_text and aspect > 1
                else None
            )
            visuals.append(worker._visual(media_id).model_copy(update={"text_column": column}))
        return tuple(visuals)

    monkeypatch.setattr(phone_visuals_mod, "bind_phone_visual_assets", bind)
    variant = worker._run(job)
    clips = {clip.id: clip for clip in worker._card_clips(_recipe(job))}
    return clips, variant, measured, worker


def test_worker_draws_wide_cards_as_strips_in_the_band_they_already_had(monkeypatch):
    off, off_variant, off_measured, worker = _strip_worker_job(monkeypatch, strip=False)
    on, on_variant, on_measured, _ = _strip_worker_job(monkeypatch, strip=True)

    assert off_measured == [False], "no strip: the bind skips the measure, as today"
    assert all(clip.source_crop is None for clip in off.values())
    assert on_measured == [True]
    assert on.keys() == off.keys() and len(on) >= 10
    assert on_variant["phone_overlay_receipt"] == off_variant["phone_overlay_receipt"]

    strips = 0
    for clip_id, clip in on.items():
        today, place = off[clip_id].visual_placement, clip.visual_placement
        assert (place.window_start, place.window_end) == (today.window_start, today.window_end)
        beside_mark = today.x_fraction != 0.5 and today.width_fraction < 0.94
        if not beside_mark:
            assert place.y_fraction == today.y_fraction, "same band"
        aspect = worker.ASPECT[clip.source_asset_id]
        if clip.source_crop is None:  # her portrait sign-up screenshot stays whole
            assert place == today and aspect < 1
            continue
        strips += 1
        assert (place.fade_in, place.fade_out) == (False, False)
        if today.x_fraction == 0.5 and today.width_fraction < 0.94 or beside_mark:
            # full width and centred; beside the Kria mark it starts below the mark instead
            assert (place.width_fraction, place.x_fraction) == (pytest.approx(0.94), 0.5)
        else:  # already as wide as its spot: the same box, the picture cut
            assert (place.width_fraction, place.x_fraction) == (
                today.width_fraction,
                today.x_fraction,
            )
        crop = clip.source_crop
        source_w, source_h = 1920, round(1920 / aspect)
        drawn_h = (
            place.width_fraction
            * (1080 / 1920)
            * (crop.height * source_h)
            / (crop.width * source_w)
        )
        today_h = today.width_fraction * (1080 / 1920) / aspect
        if beside_mark:  # moved past the mark: never taller than the card it replaces
            assert drawn_h <= today_h + 1e-4
            top = place.y_fraction - drawn_h / 2
            assert top >= 0.7745 + 0.015 - 1e-3 and top + drawn_h <= 0.98 + 1e-3
            continue
        # as tall as the whole-picture card it replaces: nothing new above or below it
        assert drawn_h == pytest.approx(today_h, abs=1e-4)
        assert crop.width < 0.5, "cut to the text column: over twice the text size"
    assert strips >= 10, "her wide screenshots and recordings are cut to their text"
