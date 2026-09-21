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


# The approved weights. Signed off on real footage; a change here should be a
# deliberate edit to both places, not a stray tweak that nobody notices.
APPROVED_WEIGHTS = {
    "mist": ("#CAD2DB", 0.42, 0.10),
    "graphite": ("#526071", 0.62, 0.08),
}


@pytest.mark.parametrize("variant", sorted(APPROVED_WEIGHTS))
def test_shipped_weights_match_the_approved_values(variant):
    assert B.VARIANTS[variant] == APPROVED_WEIGHTS[variant]


@pytest.mark.parametrize("variant", sorted(APPROVED_WEIGHTS))
def test_contrast_is_measured_for_every_recommended_pairing(variant):
    """The watermark is not gated on contrast, but it is always measured.

    The numbers are the record of what the chosen weight costs; losing them
    would make the next weight change a matter of opinion.
    """
    backgrounds = B._backgrounds()
    width = B.WATERMARK_SIZES["standard"]
    pad = B.watermark_pad(variant)
    tile = B.build_watermark(variant, width)
    bare = wordmark(width, fill=B.VARIANTS[variant][0])
    ink = B._place(B._pad(bare.full, pad), B.WATERMARK_HOME, pad)

    for bg_name in B.RECOMMENDED[variant]:
        composite = verify.over(B._place(tile, B.WATERMARK_HOME, pad),
                                backgrounds[bg_name])
        result = verify.contrast(ink, composite)
        assert result.worst_tile > 1.0, (
            f"{variant} on {bg_name} is indistinguishable from its background")


def test_the_two_tones_cover_every_footage_class_between_them():
    # An editor must never face footage with no signed-off variant.
    covered = set(B.RECOMMENDED["mist"]) | set(B.RECOMMENDED["graphite"])
    assert covered == set(B._backgrounds())


def test_every_size_sits_on_the_caption_floor():
    # Bottom-anchored, so the tallest size cannot creep under the username.
    for size in B.WATERMARK_SIZES:
        _x, y = B.watermark_slot(size, "bottom-left")
        height = wordmark(B.WATERMARK_SIZES[size]).size[1]
        assert y + height == B.WATERMARK_BOTTOM
        assert verify.is_clear((B.WATERMARK_LEFT, y,
                                B.WATERMARK_LEFT + 400, y + height))


@pytest.mark.skipif(not (DIST / "proofs" / "report.json").exists(),
                    reason="dist/ not built")
def test_committed_report_is_geometrically_clean_and_still_measured():
    report = json.loads((DIST / "proofs" / "report.json").read_text())
    assert all(v["clear"] for v in report["placements"].values())
    # Contrast is reported, not gated -- but it must be present, or the record
    # of what the chosen weight costs is gone.
    assert report["legibility"], "no legibility measurements in the report"
    assert all("worst_tile" in v for v in report["legibility"].values())
