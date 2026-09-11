"""Full cloud text-dissolve composition on deterministic synthetic glyph shapes."""

import json
import os
import sys
from itertools import groupby
from pathlib import Path

import skia

from app.pipeline.dissolve_effect import _skia_image_to_rgba_array, render_dissolve_skia_image

FIXTURE = Path(__file__).parents[1] / "fixtures/phone_dissolve_composition.json"


def reference():
    assert (
        Path(sys.modules[render_dissolve_skia_image.__module__].__file__)
        .resolve()
        .is_relative_to(Path(__file__).resolve().parents[2] / "app")
    ), "Run fixture generation with PYTHONPATH=. to use this worktree's renderer"
    width, height = 640, 480
    rectangles = [
        [80, 60, 35, 100],
        [120, 60, 95, 20],
        [120, 100, 75, 20],
        [240, 180, 160, 45],
        [470, 350, 100, 40],
    ]
    surface = skia.Surface(width, height)
    surface.getCanvas().clear(skia.ColorTRANSPARENT)
    for x, y, w, h in rectangles:
        surface.getCanvas().drawRect(
            skia.Rect.MakeXYWH(x, y, w, h), skia.Paint(Color=skia.ColorWHITE)
        )
    source = surface.makeImageSnapshot()
    cases = []
    for seed in [101, 138]:
        for time in [0, 3.02, 3.1, 3.2, 3.4, 3.7, 4]:
            image = render_dissolve_skia_image(source, time, 4, seed=seed, cap_to_webkit=True)
            pixels = _skia_image_to_rgba_array(image)
            if directory := os.environ.get("KRIA_DISSOLVE_DEBUG_DIR"):
                from PIL import Image

                Image.fromarray(pixels).save(Path(directory) / f"cloud-{seed}-{time}.png")
            cases.append(
                {
                    "seed": seed,
                    "time": time,
                    "total_alpha": int(pixels[:, :, 3].sum()),
                    "alpha_runs": [
                        [int(value), sum(1 for _ in run)]
                        for value, run in groupby(pixels[:, :, 3].ravel())
                    ],
                }
            )
    return {"width": width, "height": height, "rectangles": rectangles, "cases": cases}


def test_composition_fixture_matches_cloud():
    assert json.loads(FIXTURE.read_text()) == reference()


if __name__ == "__main__" and "--write" in sys.argv:
    FIXTURE.write_text(json.dumps(reference(), separators=(",", ":")) + "\n")
