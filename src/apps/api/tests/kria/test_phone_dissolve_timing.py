"""Native dissolve timing and particle cells reference real production output."""

import json
import sys
from pathlib import Path

import skia

from app.pipeline import dissolve_effect as cloud

FIXTURE = Path(__file__).parents[1] / "fixtures/phone_dissolve_timing.json"


def reference():
    timing = []
    particles = []
    for preset, params in [
        ("text", cloud.DEFAULT_DISSOLVE_PARAMS),
        ("media", cloud.MEDIA_OVERLAY_DISSOLVE_PARAMS),
    ]:
        for cap in [False, True]:
            for duration in [0.0005, 0.1, 1, 4]:
                for time in [-1, 0, duration / 2, duration - 0.1, duration, duration + 1]:
                    progress = cloud.dissolve_progress_at(time, duration, params)
                    timing.append(
                        {
                            "preset": preset,
                            "cap": cap,
                            "duration": duration,
                            "time": time,
                            "state": {
                                "linear_progress": cloud.dissolve_linear_progress_at(
                                    time, duration, params
                                ),
                                "progress": progress,
                                "alpha": cloud.dissolve_alpha_at_progress(progress, params),
                                "displacement_scale": cloud.dissolve_scale_at_progress(
                                    progress, params, cap_to_webkit=cap
                                ),
                                "transform_scale": cloud.dissolve_transform_scale_at_progress(
                                    progress, params, cap_to_webkit=cap
                                ),
                            },
                        }
                    )
        for seed in [0, 101, 138, 4294967295]:
            for alpha in [127, 255]:
                surface = skia.Surface(24, 18)
                surface.getCanvas().clear(skia.ColorSetARGB(alpha, 255, 255, 255))
                source = surface.makeImageSnapshot()
                for progress in [0, 0.18, 0.42, 0.6, 0.9, 1]:
                    image = cloud._apply_skia_particle_breakup(
                        source, progress, seed=seed, params=params
                    )
                    pixels = cloud._skia_image_to_rgba_array(image)
                    particles.append(
                        {
                            "preset": preset,
                            "seed": seed,
                            "source_alpha": alpha,
                            "progress": progress,
                            "cells": [
                                {
                                    "x": x,
                                    "y": y,
                                    "alpha": int(
                                        pixels[
                                            y * params.particle_cell_px,
                                            x * params.particle_cell_px,
                                            3,
                                        ]
                                    ),
                                }
                                for y in range(6)
                                for x in range(8)
                            ],
                        }
                    )
    return {"timing": timing, "particles": particles}


def test_particle_image_keeps_its_pixels_after_another_frame_is_created():
    surface = skia.Surface(24, 18)
    surface.getCanvas().clear(skia.ColorSetARGB(127, 255, 255, 255))
    source = surface.makeImageSnapshot()
    previous = None
    for progress in [0.42, 0.6, 0.9, 1]:
        image = cloud._apply_skia_particle_breakup(
            source, progress, seed=0, params=cloud.DEFAULT_DISSOLVE_PARAMS
        )
        expected = 48 if progress >= 0.9 else 127
        assert cloud._skia_image_to_rgba_array(image)[0, 6, 3] == expected
        # Keep two lazy frames alive as the renderer does while processing the next.
        previous = image
    assert cloud._skia_image_to_rgba_array(previous)[0, 6, 3] == 48


def test_dissolve_timing_and_particle_alpha_match_cloud():
    assert json.loads(FIXTURE.read_text()) == reference()


if __name__ == "__main__" and "--write" in sys.argv:
    FIXTURE.write_text(json.dumps(reference(), indent=2) + "\n")
