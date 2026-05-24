"""Tests for the pre-PR overlay render-verify gate (app/pipeline/overlay_verify.py).

Two layers, mirroring the gate's two signals:

1. DETECTOR unit tests on SYNTHETIC frames — the "MUST FAIL" cases (left/right
   edge clipping, truncated OCR text) are fed hand-built frames, so they prove
   the verifier catches the #296 class WITHOUT needing a broken renderer to exist.
   A verifier that always passes is worse than none; these are what stop that.

2. INTEGRATION tests that render the committed fixtures through the REAL Skia
   renderer and assert clean placement — the regression set for the renderer
   itself — plus a negative control (an off-canvas overlay the verifier MUST flag).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

skia = pytest.importorskip("skia")  # core dep; skip cleanly if a runner lacks it

from app.pipeline import overlay_verify as ov  # noqa: E402

CANVAS_W, CANVAS_H = ov.tos.CANVAS_W, ov.tos.CANVAS_H
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "overlay_verify"


# -- helpers: synthetic frames ------------------------------------------------


def _solid(color=(0, 0, 0)) -> Image.Image:
    return Image.new("RGB", (CANVAS_W, CANVAS_H), color)


def _frame_with_text_block(
    left, top, right, bottom, bg=(0, 0, 0), fg=(255, 255, 255)
) -> Image.Image:
    """A solid-bg frame with one opaque rectangle standing in for a text block."""
    im = _solid(bg)
    im.paste(Image.new("RGB", (right - left, bottom - top), fg), (left, top))
    return im


# =====================================================================
# 1. DETECTOR UNIT TESTS — synthetic frames, no renderer
# =====================================================================


class TestCheckClipping:
    def test_centered_block_passes(self):
        im = _frame_with_text_block(400, 800, 680, 920)
        bbox = ov.text_bbox(im, bg_hex="#000000")
        verdict, _ = ov.check_clipping(bbox, im.width, im.height)
        assert verdict == "PASS"

    def test_left_clipped_must_fail(self):
        """The #296 signature: text against the left edge (x≈0)."""
        im = _frame_with_text_block(0, 800, 300, 920)
        bbox = ov.text_bbox(im, bg_hex="#000000")
        verdict, reason = ov.check_clipping(bbox, im.width, im.height)
        assert verdict == "FAIL"
        assert "LEFT" in reason

    def test_right_clipped_must_fail(self):
        im = _frame_with_text_block(CANVAS_W - 300, 800, CANVAS_W, 920)
        bbox = ov.text_bbox(im, bg_hex="#000000")
        verdict, reason = ov.check_clipping(bbox, im.width, im.height)
        assert verdict == "FAIL"
        assert "RIGHT" in reason

    def test_empty_frame_must_fail(self):
        bbox = ov.text_bbox(_solid((0, 0, 0)), bg_hex="#000000")
        verdict, reason = ov.check_clipping(bbox, CANVAS_W, CANVAS_H)
        assert verdict == "FAIL"
        assert "no text" in reason.lower()

    def test_transparent_source_uses_alpha(self):
        """draw_frame output is transparent RGBA — the opaque pixels are the text."""
        im = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
        im.paste(Image.new("RGBA", (280, 120), (255, 255, 255, 255)), (400, 800))
        bbox = ov.text_bbox(im, bg_hex=None)
        assert bbox is not None and bbox[0] == 400
        verdict, _ = ov.check_clipping(bbox, im.width, im.height)
        assert verdict == "PASS"


class TestCheckContent:
    def test_exact_match_passes(self):
        v, ratio, _ = ov.check_content("It's not just luck", "It's not just luck")
        assert v == "PASS" and ratio == pytest.approx(1.0)

    def test_punctuation_and_case_ignored(self):
        v, _, _ = ov.check_content("its NOT just luck!!", "It's not just luck")
        assert v == "PASS"

    def test_truncation_must_fail(self):
        """The literal #296 bug: 'It's not just luck' burned as 's not just luck'."""
        v, _, reason = ov.check_content("s not just luck", "It's not just luck")
        assert v == "FAIL"
        assert "trunc" in reason.lower()

    def test_empty_ocr_with_expected_text_fails(self):
        v, _, _ = ov.check_content("", "combination of hard work")
        assert v == "FAIL"

    def test_wrong_text_fails(self):
        v, _, _ = ov.check_content("pilot in cockpit", "Rule of Thirds")
        assert v == "FAIL"

    def test_unavailable_ocr_is_skipped_not_failed(self):
        v, _, _ = ov.check_content(None, "anything")
        assert v == "SKIPPED"

    def test_verdict_mapping_is_threshold_consistent(self):
        """Whatever similarity difflib returns, the verdict bucket must match the
        thresholds — guards the WARN band without pinning a brittle exact ratio."""
        for ocr, spec in [
            ("helo wrld foo", "hello world foo bar"),
            ("motivation", "motivation matters most always"),
            ("the quick brown fox", "the quick brown fix"),
        ]:
            v, ratio, _ = ov.check_content(ocr, spec)
            if ov._normalize(ocr) in ov._normalize(spec):
                continue  # truncation path tested separately
            if ratio >= ov.SIM_WARN:
                assert v == "PASS", (ocr, ratio)
            elif ratio >= ov.SIM_FAIL:
                assert v == "WARN", (ocr, ratio)
            else:
                assert v == "FAIL", (ocr, ratio)


def test_worst_verdict_ordering():
    assert ov._worst("PASS", "FAIL") == "FAIL"
    assert ov._worst("PASS", "WARN", "SKIPPED") == "WARN"
    assert ov._worst("PASS", "SKIPPED") == "PASS"


# =====================================================================
# 2. INTEGRATION — real Skia renderer
# =====================================================================


def _fixture_overlays(name: str) -> list[dict]:
    data = json.loads((FIXTURES / name).read_text())
    return data["overlays"]


@pytest.mark.parametrize("fixture", sorted(p.name for p in FIXTURES.glob("*.json")))
def test_fixtures_render_unclipped(fixture):
    """Every committed fixture must render un-clipped through the real Skia path.
    This is the renderer's regression set — a future Skia change that re-breaks
    anchoring/wrapping fails here."""
    report = ov.verify_recipe(
        {"overlays": _fixture_overlays(fixture)},
        render_mode=ov.RENDER_DRAW_FRAME,
        run_ocr=False,
    )
    assert report.overlays, f"{fixture} produced no overlays"
    for o in report.overlays:
        assert o.clipping == "PASS", f"{fixture} ov{o.overlay_index}: {o.reasons}"


def test_negative_control_offcanvas_overlay_is_flagged():
    """An overlay positioned off the right edge MUST be flagged by the verifier.
    Proves the integration path actually fails on a genuinely clipped render —
    not just on synthetic detector inputs."""
    overlay = {
        "text": "this text is pushed off the canvas",
        "effect": "none",
        "text_size_px": 110,
        "position_x_frac": 1.4,
        "position_y_frac": 0.5,
        "text_color": "#FFFFFF",
        "text_anchor": "left",
        "start_s": 0.0,
        "end_s": 2.0,
    }
    res = ov.verify_overlay(overlay, render_mode=ov.RENDER_DRAW_FRAME, run_ocr=False)
    assert res.clipping == "FAIL", res.reasons


def test_player_card_effect_is_skipped():
    res = ov.verify_overlay(
        {"text": "x", "effect": "player-card", "start_s": 0, "end_s": 1},
        render_mode=ov.RENDER_DRAW_FRAME,
        run_ocr=False,
    )
    assert res.verdict == "SKIPPED"


def test_empty_text_overlay_is_skipped():
    res = ov.verify_overlay(
        {"text": "  ", "effect": "none", "start_s": 0, "end_s": 1},
        render_mode=ov.RENDER_DRAW_FRAME,
        run_ocr=False,
    )
    assert res.verdict == "SKIPPED"


def test_iter_recipe_overlays_shapes():
    assert len(ov.iter_recipe_overlays([{"text": "a"}, {"text": "b"}])) == 2
    assert len(ov.iter_recipe_overlays({"overlays": [{"text": "a"}]})) == 1
    slots = {
        "slots": [
            {"text_overlays": [{"text": "a"}, {"text": "b"}]},
            {"text_overlays": [{"text": "c"}]},
        ]
    }
    out = ov.iter_recipe_overlays(slots)
    assert [(s, o) for s, o, _ in out] == [(0, 0), (0, 1), (1, 0)]
    with pytest.raises(ValueError):
        ov.iter_recipe_overlays({"nope": 1})
