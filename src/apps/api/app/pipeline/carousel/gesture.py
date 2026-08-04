"""Scripted drag+release gesture traces used to drive the carousel spring simulation.

A GestureTrace is the deterministic input fixture for `spring.simulate`: a
frame-indexed sequence of pointer deltas while dragging, followed by a release.
Golden-trace tests pin `spring.simulate(CANONICAL_FLICK, ...)` output.
"""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class GestureTrace:
    """A scripted drag+release, frame-indexed at `fps`. drag_deltas_px are per-frame
    pointer Δx while dragging (negative = drag left / advance carousel);
    release happens after the last delta."""

    drag_deltas_px: tuple[float, ...]
    fps: int = 30


CANONICAL_FLICK = GestureTrace(
    drag_deltas_px=(-4, -6, -9, -13, -18, -24, -31, -39, -48, -58, -69, -81),
    fps=30,
)


def dump_json(trace: GestureTrace = CANONICAL_FLICK) -> str:
    payload = {"drag_deltas_px": list(trace.drag_deltas_px), "fps": trace.fps}
    return json.dumps(payload, indent=2) + "\n"


if __name__ == "__main__":
    print(dump_json())
