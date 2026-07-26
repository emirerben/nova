from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.pipeline.handwriting_strokes import (
    handwriting_asset,
    layout_handwriting_text,
    partial_polyline,
    stroke_local_progress,
)


def test_browser_and_renderer_share_byte_identical_stroke_assets() -> None:
    api_root = Path(__file__).resolve().parents[2]
    api_asset = api_root / "assets/fonts/handwriting-strokes.json"
    web_asset = api_root.parent / "web/src/data/handwriting-strokes.json"

    assert api_asset.read_bytes() == web_asset.read_bytes()


def test_stroke_asset_covers_ascii_latin_and_turkish() -> None:
    glyphs = handwriting_asset()["glyphs"]
    for char in "AaZz09!? ÇçĞğİıÖöŞşÜü":
        assert char in glyphs
        if not char.isspace():
            assert glyphs[char]["paths"]


def test_layout_writes_one_glyph_after_another_and_wraps() -> None:
    layout = layout_handwriting_text(
        "WRITE THIS NOW",
        max_width_em=4.0,
        letter_spacing_em=0.04,
        line_spacing=1.4,
    )

    assert len(layout.lines) >= 2
    assert layout.width_em <= 4.0 + 1e-6
    assert layout.line_step_em > layout.ascent_em
    assert layout.strokes
    assert all(
        first.end_progress <= second.start_progress
        for first, second in zip(layout.strokes, layout.strokes[1:], strict=False)
    )
    assert layout.strokes[-1].end_progress < 1.0


def test_stroke_progress_and_partial_polyline_are_monotonic() -> None:
    layout = layout_handwriting_text("A", max_width_em=8.0)
    stroke = layout.strokes[0]

    assert stroke_local_progress(stroke, stroke.start_progress) == 0
    assert stroke_local_progress(stroke, stroke.end_progress) == 1
    midpoint = (stroke.start_progress + stroke.end_progress) / 2
    assert stroke_local_progress(stroke, midpoint) == pytest.approx(0.5)

    points = ((0.0, 0.0), (1.0, 0.0), (2.0, 0.0))
    assert partial_polyline(points, 0) == ()
    assert partial_polyline(points, 0.25) == ((0.0, 0.0), (0.5, 0.0))
    assert partial_polyline(points, 1) == points


def test_generated_asset_has_stable_schema() -> None:
    asset = handwriting_asset()
    assert asset["version"] == 1
    assert asset["source"].startswith("Patrick Hand")
    assert 0.08 <= asset["stroke_width"] <= 0.12
    # Guard against accidentally committing pretty-printed or partial output.
    encoded = json.dumps(asset, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    assert 250_000 <= len(encoded) <= 400_000
