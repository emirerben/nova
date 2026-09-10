"""Pin discrete text/cursor samples to the production animation dispatcher."""

import json
import sys
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from app.pipeline.text_motion_v2 import normalize_text_motion
from app.pipeline.text_overlay_skia import _draw_with_animation, _fixed_reveal_lines

FIXTURE = Path(__file__).parents[1] / "fixtures/phone_text_reveal.json"


def reference():
    cases = []
    for effect in ["typewriter", "stream-in"]:
        for text in ["Hello brave world", "  İyi   günler\nCafe\u0301 👨‍👩‍👧‍👦!", "A\n\nB", ""]:
            for index in range(5):
                motion = (
                    None
                    if index == 0
                    else asdict(
                        normalize_text_motion(
                            effect,
                            {
                                "version": 2,
                                "speed": [1, 0.25, 0.53, 1, 4][index],
                                "intensity": [1, 0, 0.3, 0.8, 1][index],
                                "cursor_style": ["bar", "none", "block", "underscore", "bar"][
                                    index
                                ],
                                "cursor_blink_ms": 173,
                            },
                        )
                    )
                )
                schedule = [2.1, 2.5, 3.0015, 2.1] if index == 0 else None
                samples = []
                for time in [0, 1 / 30, 0.083333, 0.1, 0.166667, 0.25, 0.5, 0.9, 1.002, 2, 5]:
                    with patch("app.pipeline.text_overlay_skia._draw_centered_text") as draw:
                        _draw_with_animation(
                            None,
                            {
                                "text": text,
                                "effect": effect,
                                "start_s": 2,
                                "motion": {"version": 2, **motion} if motion else None,
                                "reveal_schedule_s": schedule,
                            },
                            time,
                            6,
                        )
                    samples.append(
                        {
                            "time": time,
                            "visible_text": draw.call_args.args[1],
                            "show_cursor": draw.call_args.kwargs["show_cursor"],
                            "cursor_style": draw.call_args.kwargs["cursor_style"],
                        }
                    )
                cases.append(
                    {
                        "effect": effect,
                        "text": text,
                        "start": 2,
                        "schedule": schedule,
                        "motion": motion,
                        "samples": samples,
                    }
                )
    return cases


def test_reference_matches_cloud():
    assert json.loads(FIXTURE.read_text()) == reference()


def test_fixed_line_reveal_keeps_full_geometry():
    assert _fixed_reveal_lines(["Hello", "brave world"], "Hello bra") == (["Hello", "bra"], 1)


if __name__ == "__main__" and "--write" in sys.argv:
    FIXTURE.write_text(json.dumps(reference(), indent=2, ensure_ascii=False) + "\n")
