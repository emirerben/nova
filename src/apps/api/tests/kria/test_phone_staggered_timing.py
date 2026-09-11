"""Pin the glyph-build state to production, including short legacy windows."""

import json
import sys
from dataclasses import asdict
from pathlib import Path

from app.pipeline.text_motion_v2 import normalize_text_motion
from app.pipeline.text_overlay_skia import _staggered_slice_state

FIXTURE = Path(__file__).parents[1] / "fixtures/phone_staggered_timing.json"


def reference():
    cases = []
    for text in [
        "",
        "A",
        "Word",
        "First remainder words",
        "İyi günler\nCafé",
        "A\n\nB\nC\nD\nE\nF\nG\nH\nI",
        " 👩🏽‍💻  é\nשלום ",
    ]:
        for index in range(5):
            motion = (
                None
                if index == 0
                else asdict(
                    normalize_text_motion(
                        "staggered-slice",
                        {
                            "version": 2,
                            "speed": [1, 0.25, 0.53, 1, 4][index],
                            "intensity": [1, 0, 0.3, 0.8, 1][index],
                        },
                    )
                )
            )
            for duration in [0.005, 0.4, 4]:
                times = sorted(
                    set([0, 1 / 30, 0.1, 0.16, 0.55, 0.95, 1.35, 1.85, 2.4, 4, 10, duration])
                )
                cases.append(
                    {
                        "text": text,
                        "duration": duration,
                        "motion": motion,
                        "samples": [
                            {
                                "time": time,
                                "state": asdict(
                                    _staggered_slice_state(
                                        text,
                                        time,
                                        duration,
                                        {"version": 2, **motion} if motion else None,
                                    )
                                ),
                            }
                            for time in times
                        ],
                    }
                )
    return cases


def test_staggered_timing_matches_cloud():
    assert json.loads(FIXTURE.read_text()) == json.loads(json.dumps(reference()))


if __name__ == "__main__" and "--write" in sys.argv:
    FIXTURE.write_text(json.dumps(reference(), ensure_ascii=False, indent=2) + "\n")
