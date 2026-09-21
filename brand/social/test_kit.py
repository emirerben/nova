"""Invariants for the social kit. Run with the API virtualenv:

    src/apps/api/.venv/bin/python -m pytest brand/social -q

`build.py` already gates on these when it runs; these tests exist so the rules
can be checked without a full rebuild, and so a change to the wordmark source
or the chrome map fails loudly.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build as B  # noqa: E402
import verify  # noqa: E402
from kria_brand import FAVICON, TEXT_SAFE, TOP_BAND_SAFE, wordmark  # noqa: E402

DIST = Path(__file__).resolve().parent / "dist"


def test_safe_rectangles_are_clear_on_every_platform():
    assert verify.is_clear(TEXT_SAFE)
    assert verify.is_clear(TOP_BAND_SAFE)


def test_occlusion_map_actually_occludes():
    # Guards against a map that was accidentally emptied: a point inside the
    # right action rail must collide on all three platforms.
    hits = verify.check_rect((950, 1200, 1000, 1250))
    assert all(hits.values()), hits


def test_wordmark_is_derived_from_the_product_favicon():
    assert FAVICON.exists(), "the kit's only source of geometry is missing"
    mark = wordmark(400)
    assert len(mark.letters) == 4
    # Letters must share the full mark's coordinate space, or the outro would
    # animate them into the wrong positions.
    assert all(letter.shape == mark.full.shape for letter in mark.letters)

    # Compositing the four layers must reproduce the single-pass mark. Alpha
    # combines as a+b-ab where the glyphs overlap, so this is a real check on
    # the split, not just a bounding-box one. Antialiased edges are allowed a
    # one-level rounding difference.
    composited = np.zeros(mark.full.shape[:2], dtype=np.float64)
    for letter in mark.letters:
        a = letter[..., 3].astype(np.float64) / 255.0
        composited = composited + a - composited * a
    delta = np.abs(composited * 255.0 - mark.full[..., 3].astype(np.float64))
    assert delta.max() <= 1.5, delta.max()


@pytest.mark.parametrize("variant", sorted(B.RECOMMENDED))
def test_recommended_pairings_clear_the_contrast_floor(variant):
    backgrounds = B._backgrounds()
    width = B.WATERMARK_SIZES["standard"]
    tile = B.build_watermark(variant, width)
    bare = wordmark(width, fill=B.VARIANTS[variant][0])
    ink = B._overlay_rgba(
        B._pad(bare.full, B.watermark_pad(variant)), *B.WATERMARK_HOME)

    for bg_name in B.RECOMMENDED[variant]:
        composite = verify.over(B._overlay_rgba(tile, *B.WATERMARK_HOME),
                                backgrounds[bg_name])
        result = verify.contrast(ink, composite)
        assert result.passes_floor, (
            f"{variant} on {bg_name}: worst tile {result.worst_tile:.2f}:1 "
            f"< {verify.CONTRAST_FLOOR}:1")


def test_plate_is_the_universal_variant():
    # The kit's headline promise: one file an editor can reach for blindly.
    assert set(B.RECOMMENDED["plate"]) == set(B._backgrounds())


@pytest.mark.skipif(not (DIST / "proofs" / "report.json").exists(),
                    reason="dist/ not built")
def test_committed_report_has_no_gated_failures():
    report = json.loads((DIST / "proofs" / "report.json").read_text())
    failed = [k for k, v in report["legibility"].items()
              if v["gated"] and not v["passes_floor"]]
    assert not failed, failed
    assert all(v["clear"] for v in report["placements"].values())
