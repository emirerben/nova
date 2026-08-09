"""Dump ground-truth fixtures for the TS carousel-choreography parity test.

Runs `choreography.build_timeline`/`choreography.rolling_timeline` (the
Python carousel engine — see `app/pipeline/carousel/choreography.py`) over a
fixed, representative configuration and emits the resulting `FrameState`
lists as JSON, rounded to 6 decimal places. Output is the committed fixture
`src/apps/api/tests/pipeline/carousel/choreography_traces.json`, consumed by
BOTH sides of the parity contract:
  - TS: src/apps/web/src/lib/carousel-preview/__tests__/choreography.test.ts
  - Python: tests/pipeline/carousel/test_choreography_fixture.py (pins the
    Python builders still match the checked-in fixture, so it can't silently
    drift out from under the TS port without a test failing on THIS side
    too).

Regenerate when choreography.py's timeline-authoring math changes::

    cd src/apps/api && \\
      .venv/bin/python scripts/dev/dump_choreography_fixture.py \\
      > tests/pipeline/carousel/choreography_traces.json
"""

from __future__ import annotations

import json

from app.pipeline.carousel.choreography import FocusMoment, build_timeline, rolling_timeline
from app.pipeline.carousel.effects import CardGeometry

GEO = CardGeometry(card_w=540, card_h=720, gap=48, corner_radius=24)
VIEWPORT_W = 1080
N_CARDS = 4
SEED = 0


def _round6(x: float) -> float:
    return round(x, 6)


def _dump_frames(frames) -> list[dict]:
    return [
        {
            "t_s": _round6(f.t_s),
            "scroll_x": _round6(f.scroll_x),
            "focus_card": f.focus_card,
            "focus_t": _round6(f.focus_t),
            "dim": _round6(f.dim),
        }
        for f in frames
    ]


def main() -> dict:
    build_frames = build_timeline(
        N_CARDS,
        GEO,
        VIEWPORT_W,
        focus_moments=(FocusMoment(card_index=1),),
        seed=SEED,
    )
    rolling_frames = rolling_timeline(
        N_CARDS,
        GEO,
        VIEWPORT_W,
        duration_s=6.0,
        seed=SEED,
    )
    return {
        "geometry": {
            "card_w": GEO.card_w,
            "card_h": GEO.card_h,
            "gap": GEO.gap,
            "corner_radius": GEO.corner_radius,
        },
        "viewport_w": VIEWPORT_W,
        "n_cards": N_CARDS,
        "seed": SEED,
        "build_timeline": {
            "focus_moments": [{"card_index": 1, "hold_s": 2.0, "zoom_s": 0.6}],
            "frames": _dump_frames(build_frames),
        },
        "rolling_timeline": {
            "duration_s": 6.0,
            "frames": _dump_frames(rolling_frames),
        },
    }


if __name__ == "__main__":
    print(json.dumps(main(), indent=2))
