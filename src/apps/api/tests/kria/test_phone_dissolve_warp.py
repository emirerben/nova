"""Nearest-sampled production displacement, before transform/particle composition."""

import json
import sys
from pathlib import Path

import numpy as np
import skia

from app.pipeline.dissolve_effect import (
    DEFAULT_DISSOLVE_PARAMS,
    _numpy_rgba_to_skia_image,
    _skia_displacement_filter,
    _skia_image_to_rgba_array,
)

FIXTURE = Path(__file__).parents[1] / "fixtures/phone_dissolve_warp.json"


def reference():
    width, height = 256, 384
    yy, xx = np.indices((height, width))
    rgba = np.stack([xx % 251, yy % 251, (xx + yy) % 251, np.full_like(xx, 255)], axis=-1).astype(
        np.uint8
    )
    source = _numpy_rgba_to_skia_image(rgba)
    cases = []
    for seed in [0, 101, 138]:
        for scale in [0, 30, 130, 400]:
            surface = skia.Surface(width, height)
            image_filter = _skia_displacement_filter(
                source,
                width=width,
                height=height,
                scale_px=scale,
                seed=seed,
                params=DEFAULT_DISSOLVE_PARAMS,
            )
            surface.getCanvas().drawPaint(skia.Paint(ImageFilter=image_filter))
            pixels = _skia_image_to_rgba_array(surface.makeImageSnapshot())
            cases.append(
                {
                    "seed": seed,
                    "scale": scale,
                    "pixels": [
                        {"x": x, "y": y, "rgba": pixels[y, x].tolist()}
                        for y in [0, 1, 7, 30, 63, 127, 190, 220, 255, 300, 360, 383]
                        for x in [0, 1, 7, 30, 63, 127, 190, 255]
                    ],
                }
            )
    return {"width": width, "height": height, "cases": cases}


def test_warp_fixture_matches_cloud():
    assert json.loads(FIXTURE.read_text()) == reference()


if __name__ == "__main__" and "--write" in sys.argv:
    FIXTURE.write_text(json.dumps(reference(), indent=2) + "\n")
