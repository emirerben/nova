/**
 * Scripted drag+release gesture traces used to drive the carousel spring
 * simulation. Mirrors app/pipeline/carousel/gesture.py.
 *
 * A GestureTrace is the deterministic input fixture for `spring.simulate`: a
 * frame-indexed sequence of pointer deltas while dragging, followed by a
 * release. Golden-trace tests pin `simulate(CANONICAL_FLICK, ...)` output.
 */

import type { GestureTrace } from "./types";

// Mirrors app/pipeline/carousel/gesture.py:CANONICAL_FLICK — keep in sync
// (golden trace test pins this). drag_deltas_px are per-frame pointer Δx
// while dragging (negative = drag left / advance carousel); release happens
// after the last delta.
export const CANONICAL_FLICK: GestureTrace = {
  dragDeltasPx: [-4, -6, -9, -13, -18, -24, -31, -39, -48, -58, -69, -81],
  fps: 30,
};
