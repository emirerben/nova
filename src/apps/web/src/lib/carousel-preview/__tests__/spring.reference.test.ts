/**
 * Port of test_spring_golden_trace.py's Section 1 (hand-derived reference
 * values for damp/project/release/isSettled) — same literals, same
 * comments, TS side of the parity contract.
 */

import { FRICTION, damp, isSettled, project, release } from "../spring";
import { createSpringState } from "../types";

describe("damp reference values", () => {
  it("single reference frame (delta_ms == 1000/60) collapses the exponent to exactly t", () => {
    // damp(0, 100, 0.12, 1000/60) == 0 + 100 * 0.12 == 12.0
    expect(damp(0.0, 100.0, 0.12, 1000 / 60)).toBeCloseTo(12.0, 9);
  });

  it("two reference frames: smoothing factor is 1 - (1 - t)**2", () => {
    const expected = 100 * (1 - (1 - 0.12) ** 2);
    expect(expected).toBeCloseTo(22.56, 9);
    expect(damp(0.0, 100.0, 0.12, 2 * (1000 / 60))).toBeCloseTo(expected, 9);
  });
});

describe("project reference value", () => {
  it("project(0, 28, 0.72) == 100", () => {
    expect(project(0.0, 28.0, 0.72)).toBeCloseTo(100.0, 9);
  });
});

describe("release reference case", () => {
  it("state(target=0, velocity=50, totalDragPx=400) picks snap 588 with the expected force", () => {
    // state(target=0, velocity=50, total_drag_px=400 — a real flick, well
    // past DRAG_ACTIVATION_PX=10, so release()'s snap-seeking logic
    // actually runs), snap_positions=[0, 588, 1176], snapport=1080.
    //   velocity * 2        = 100
    //   resting_x           = project(0, 100, 0.72) = 0 + 100 / 0.28 ≈ 357.142857
    //   threshold           = 1080 / 3 = 360
    //   candidates within threshold of resting_x: 0 (|0-357.14|=357.14<=360)
    //                                              588 (|588-357.14|=230.86<=360)
    //   nearest candidate to resting_x -> 588
    //   force = (588 - 0) * (1 - 0.72) * (1 / 0.72) = 588 * 0.28 / 0.72 ≈ 228.6667
    const state = createSpringState({
      target: 0.0,
      velocity: 50.0,
      virtualScroll: 0.0,
      isDragging: true,
      totalDragPx: 400.0,
    });
    const restingX = project(0.0, 100.0, FRICTION);
    expect(restingX).toBeCloseTo(357.142857, 6);

    const result = release(state, [0.0, 588.0, 1176.0], 1080.0);

    expect(result.isDragging).toBe(false);
    expect(result.velocity).toBeCloseTo((588 * 0.28) / 0.72, 6);
    expect(result.velocity).toBeCloseTo(228.666667, 6);
    // target/virtualScroll are untouched by release().
    expect(result.target).toBeCloseTo(0.0, 9);
    expect(result.virtualScroll).toBeCloseTo(0.0, 9);
  });

  it("below DRAG_ACTIVATION_PX is a no-op tap", () => {
    // Bundle: `_.x <= 10` (P()'s bail-out) skips the whole snap-seeking
    // dance for a sub-DRAG_ACTIVATION_PX gesture — just stops dragging
    // as-is.
    const state = createSpringState({
      target: 12.0,
      velocity: 3.0,
      virtualScroll: 5.0,
      isDragging: true,
      totalDragPx: 4.0,
    });

    const result = release(state, [0.0, 588.0, 1176.0], 1080.0);

    expect(result.isDragging).toBe(false);
    expect(result.velocity).toBeCloseTo(3.0, 9);
    expect(result.target).toBeCloseTo(12.0, 9);
    expect(result.virtualScroll).toBeCloseTo(5.0, 9);
  });

  it("clamps the chosen snap to bounds", () => {
    // Bundle's ne(): the resolved snap target is clamped to
    // [min(0,(scrollWidth-scrollerWidth)*dir), max(...)] before becoming a
    // release force. nearest snap to a huge resting_x is 1176, but bounds
    // cap it at 900 — force must be computed from the CLAMPED value.
    const state = createSpringState({
      target: 0.0,
      velocity: 1000.0,
      virtualScroll: 0.0,
      isDragging: true,
      totalDragPx: 400.0,
    });

    const result = release(state, [0.0, 588.0, 1176.0], 1080.0, [0.0, 900.0]);

    const expectedForce = (900.0 - 0.0) * (1 - FRICTION) * (1 / FRICTION);
    expect(result.velocity).toBeCloseTo(expectedForce, 6);
  });
});

describe("isSettled rounds to twelve places", () => {
  it("matches Python's round(velocity, 12) == 0 semantics", () => {
    expect(isSettled(createSpringState({ velocity: 0.0 }))).toBe(true);
    expect(isSettled(createSpringState({ velocity: 4.4e-13 }))).toBe(true);
    expect(isSettled(createSpringState({ velocity: 1e-9 }))).toBe(false);
  });
});

describe("release force recurrence proof holds numerically", () => {
  it("target_n = slide_x - (slide_x - target_0) * FRICTION**n", () => {
    const target0 = 400.0;
    const slideX = 1176.0;
    const force = (slideX - target0) * (1 - FRICTION) * (1 / FRICTION);

    let state = createSpringState({
      target: target0,
      velocity: force,
      virtualScroll: 0.0,
      isDragging: false,
    });
    for (let n = 1; n <= 20; n += 1) {
      const velocity = state.velocity * FRICTION;
      const target = state.target + velocity;
      state = createSpringState({ target, velocity, virtualScroll: 0.0, isDragging: false });
      const expected = slideX - (slideX - target0) * FRICTION ** n;
      expect(state.target).toBeCloseTo(expected, 6);
      expect(state.target).toBeLessThan(slideX + 1e-9);
    }
  });
});
