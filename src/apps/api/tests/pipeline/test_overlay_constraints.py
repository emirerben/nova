"""Tests for the universal text-overlay constraint pass."""

from __future__ import annotations

from app.pipeline.overlay_constraints import _MIN_FONT_SIZE, apply_overlay_constraints
from app.pipeline.text_overlay import CANVAS_W


def _ov(text: str, **kw) -> dict:
    base = {
        "text": text,
        "start_s": 0.0,
        "end_s": 3.0,
        "font_family": "Montserrat",
        "text_size_px": 250,
        "position": "center",
        "text_anchor": "center",
    }
    base.update(kw)
    return base


def test_long_text_shrinks() -> None:
    ov = _ov(
        "This is a very long hook line that absolutely will not fit at 250px", text_size_px=250
    )
    [out] = apply_overlay_constraints([ov])
    assert out["text_size_px"] < 250
    assert out["text_size_px"] >= _MIN_FONT_SIZE


def test_short_text_unchanged() -> None:
    ov = _ov("Hi", text_size_px=120)
    [out] = apply_overlay_constraints([ov])
    # Comfortably fits → no shrink.
    assert out["text_size_px"] == 120


def test_fits_within_safe_width_after_pass() -> None:
    from app.pipeline.text_overlay import wrap_and_measure

    ov = _ov("Supercalifragilisticexpialidocious moments everywhere today", text_size_px=200)
    [out] = apply_overlay_constraints([ov])
    _, widest = wrap_and_measure(
        out["text"],
        font_family="Montserrat",
        text_size_px=out["text_size_px"],
        max_width_px=int(0.88 * CANVAS_W),
    )
    # After the pass the widest line is within the safe zone (or the floor was hit).
    assert widest <= 0.88 * CANVAS_W or out["text_size_px"] == _MIN_FONT_SIZE


def test_vertical_clamp_into_safe_zone() -> None:
    # A big block pinned at the very bottom should be pulled up.
    ov = _ov("Bottom line text", text_size_px=120, position_y_frac=0.99)
    [out] = apply_overlay_constraints([ov])
    assert out["position_y_frac"] < 0.99


def test_idempotent() -> None:
    ov = _ov(
        "This is a very long hook line that absolutely will not fit at 250px", text_size_px=250
    )
    [once] = apply_overlay_constraints([dict(ov)])
    [twice] = apply_overlay_constraints([dict(once)])
    assert once["text_size_px"] == twice["text_size_px"]


def test_empty_text_skipped() -> None:
    ov = _ov("   ", text_size_px=250)
    [out] = apply_overlay_constraints([ov])
    assert out["text_size_px"] == 250


def test_does_not_rewrite_text() -> None:
    ov = _ov("Some overflowing caption text that must be shrunk to fit nicely", text_size_px=250)
    original = ov["text"]
    [out] = apply_overlay_constraints([ov])
    assert out["text"] == original
