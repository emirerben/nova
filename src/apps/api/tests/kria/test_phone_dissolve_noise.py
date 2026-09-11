"""Sample the actual Skia shader, including alpha and raster coordinate offsets."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import skia

from app.pipeline.dissolve_effect import (
    DEFAULT_DISSOLVE_PARAMS,
    _skia_displacement_filter,
    _skia_noise_image,
)

FIXTURE = Path(__file__).parents[1] / "fixtures/phone_dissolve_noise.json"


def reference():
    cases = []
    maps = []
    width, height = 1080, 1920
    for seed in [0, 101, 138, 1644, 4294967295]:
        for frequency in [0.004, 1.0, 0.125]:
            image = _skia_noise_image(width, height, frequency, seed)
            info = skia.ImageInfo.Make(
                width,
                height,
                skia.ColorType.kRGBA_8888_ColorType,
                skia.AlphaType.kPremul_AlphaType,
            )
            pixels = bytearray(width * height * 4)
            assert image.readPixels(info, pixels, width * 4, 0, 0)
            cases.append(
                {
                    "seed": seed,
                    "frequency": frequency,
                    "pixels": [
                        {
                            "x": x,
                            "y": y,
                            "rgba": list(pixels[(y * width + x) * 4 : (y * width + x) * 4 + 4]),
                        }
                        for y in [0, 1, 7, 127, 255, 499, 1000, 1919]
                        for x in [0, 1, 7, 127, 249, 255, 500, 1079]
                    ],
                }
            )
        # Capture and render the real displacement input, including Skia's
        # color matrix and source-over merge (not a reimplementation in Python).
        with patch.object(
            skia.ImageFilters,
            "DisplacementMap",
            side_effect=lambda r, g, scale, displacement, color, crop: displacement,
        ):
            displacement = _skia_displacement_filter(
                image,
                width=width,
                height=height,
                scale_px=920,
                seed=seed,
                params=DEFAULT_DISSOLVE_PARAMS,
            )
        surface = skia.Surface(width, height)
        surface.getCanvas().drawPaint(skia.Paint(ImageFilter=displacement))
        pixels = bytearray(width * height * 4)
        assert surface.makeImageSnapshot().readPixels(info, pixels, width * 4, 0, 0)
        maps.append(
            {
                "seed": seed,
                "pixels": [
                    {
                        "x": x,
                        "y": y,
                        "rgba": list(pixels[(y * width + x) * 4 : (y * width + x) * 4 + 4]),
                    }
                    for y in [0, 1, 7, 127, 255, 499, 1000, 1919]
                    for x in [0, 1, 7, 127, 249, 255, 500, 1079]
                ],
            }
        )
    return {"cases": cases, "maps": maps}


def test_noise_fixture_matches_cloud():
    assert json.loads(FIXTURE.read_text()) == reference()


if __name__ == "__main__" and "--write" in sys.argv:
    FIXTURE.write_text(json.dumps(reference(), indent=2) + "\n")
