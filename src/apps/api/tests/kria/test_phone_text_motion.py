"""Cross-runtime timing oracle. Regenerate with this file's --write entrypoint."""

import json
import sys
from dataclasses import asdict
from pathlib import Path

from app.agents._schemas.text_element import _ALLOWED_EFFECTS
from app.pipeline.text_motion_v2 import (
    authored_motion_time_s,
    normalize_text_motion,
    renderer_settle_duration_s,
    settle_duration_s,
    smooth_type_line_progresses,
    smooth_type_state_at,
    total_duration_s,
)

FIXTURE = Path(__file__).parents[1] / "fixtures/phone_text_motion_v2.json"


def reference_cases():
    inputs = [(effect, "Hello\nworld", {}) for effect in sorted(_ALLOWED_EFFECTS | {"lyric-line"})]
    for index, text in enumerate(
        ["İstanbul\nçok güzel", "مرحبا\nبالعالم", "नमस्ते दुनिया", "👩🏽‍🚀 café\ne\u0301", "", "\n\n"]
    ):
        inputs.append(
            (
                "smooth-type",
                text,
                {
                    "version": 2,
                    "speed": [0.25, 4, 0.53][index % 3],
                    "easing": ["linear", "ease-out-cubic", "ease-in-out-cubic"][index % 3],
                    "order": ["forward", "reverse", "center-out"][index % 3],
                    "direction": ["left", "right", "up", "down", "none"][index % 5],
                    "intensity": index / 5,
                    "travel_px": 150,
                    "stagger_ms": [0, 250, 33][index % 3],
                    "hold_s": 2.5,
                    "exit_s": 0.5,
                },
            )
        )
    cases = []
    for effect, text, raw in inputs:
        motion = asdict(normalize_text_motion(effect, raw))
        normalized = {"version": 2, **motion}
        settle = renderer_settle_duration_s(effect, text, normalized)
        samples = []
        for time in [-0.1, 0, 1 / 30, 0.1, settle / 2, settle - 1e-7, settle, settle + 1]:
            sample = {
                "time": time,
                "authored_time": authored_motion_time_s(effect, text, time, normalized),
            }
            if effect == "smooth-type":
                sample["smooth"] = asdict(smooth_type_state_at(text, time, normalized))
                sample["line_progresses"] = smooth_type_line_progresses(
                    text.split("\n"), time, normalized
                )
            samples.append(sample)
        cases.append(
            {
                "effect": effect,
                "text": text,
                "motion": motion,
                "settle_duration": settle_duration_s(effect, text, normalized),
                "renderer_settle_duration": settle,
                "total_duration": total_duration_s(effect, text, normalized),
                "samples": samples,
            }
        )
    return cases


def test_native_text_motion_fixture_matches_python_renderer():
    assert json.loads(FIXTURE.read_text()) == reference_cases()


if __name__ == "__main__" and sys.argv[1:] == ["--write"]:
    FIXTURE.write_text(json.dumps(reference_cases(), ensure_ascii=False, indent=2) + "\n")
