"""Capture real Skia dispatch transforms for the native whole-layer sampler."""

import json
import sys
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from app.pipeline.text_motion_v2 import normalize_text_motion
from app.pipeline.text_overlay_skia import _draw_with_animation

FIXTURE = Path(__file__).parents[1] / "fixtures/phone_text_transforms_v2.json"


def reference_cases(*, legacy=False):
    cases = []
    for effect in [
        "static",
        "none",
        "fade-in",
        "scale-up",
        "slide-up",
        "slide-down",
        "pop-in",
        "bounce",
        "ink-reveal",
        "handwriting",
        "typewriter",
        "stream-in",
        "smooth-type",
    ]:
        for index in range(4):
            motion = asdict(
                normalize_text_motion(
                    effect,
                    {
                        "version": 2,
                        "speed": [0.25, 0.53, 1, 4][index],
                        "intensity": [0, 0.3, 0.8, 1][index],
                        "easing": ["linear", "ease-out-cubic", "ease-in-out-cubic", "linear"][
                            index
                        ],
                        "direction": ["left", "right", "up", "down"][index],
                        "travel_px": 137,
                        "blur_px": 7,
                        "overshoot": [0, 0.15, 0.5, 1][index],
                        "exit_s": [0, 0.01, 0.5, 2][index],
                    },
                )
            )
            if legacy:
                motion = None
            duration = [0.005, 0.12, 0.25, 2][index] if legacy else [0.12, 0.75, 2, 4][index]
            samples = []
            for time in sorted(
                set(
                    [
                        0,
                        1 / 30,
                        0.1,
                        0.15,
                        0.2,
                        0.25,
                        0.4,
                        0.8,
                        duration / 2,
                        max(0, duration - 1 / 30),
                        duration,
                    ]
                )
            ):
                target = (
                    "_draw_handwriting_strokes"
                    if effect == "handwriting"
                    else "_draw_centered_text"
                )
                with patch(f"app.pipeline.text_overlay_skia.{target}") as draw:
                    _draw_with_animation(
                        None,
                        {
                            "text": "Hello",
                            "effect": effect,
                            "motion": {"version": 2, **motion} if motion else None,
                        },
                        time,
                        duration,
                    )
                kwargs = draw.call_args.kwargs
                if effect == "handwriting":
                    kwargs.update(
                        scale=1.0,
                        x_translate=0.0,
                        y_translate=0.0,
                        reveal_progress=draw.call_args.args[3],
                    )
                samples.append(
                    {
                        "time": time,
                        "state": {
                            key: kwargs.get(key, 0)
                            for key in [
                                "alpha",
                                "scale",
                                "x_translate",
                                "y_translate",
                                "reveal_progress",
                                "blur_px",
                            ]
                        },
                    }
                )
            cases.append(
                {
                    "effect": effect,
                    "text": "Hello",
                    "duration": duration,
                    "motion": motion,
                    "samples": samples,
                }
            )
    return cases


def test_native_transform_reference_is_current():
    assert json.loads(FIXTURE.read_text()) == reference_cases()


LEGACY_FIXTURE = FIXTURE.with_name("phone_text_transforms_legacy.json")


def test_native_legacy_transform_reference_is_current():
    assert json.loads(LEGACY_FIXTURE.read_text()) == reference_cases(legacy=True)


if __name__ == "__main__" and "--write" in sys.argv:
    FIXTURE.write_text(json.dumps(reference_cases(), indent=2) + "\n")
    LEGACY_FIXTURE.write_text(json.dumps(reference_cases(legacy=True), indent=2) + "\n")
