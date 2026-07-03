import {
  fitPxPerSecond,
  clampPxPerSecond,
  secondsToPx,
  pxToSeconds,
  scaledTrackWidth,
  tickIntervalForScale,
  rulerTicks,
  MIN_PX_PER_SECOND,
  MAX_PX_PER_SECOND,
} from "../timeline-scale";

describe("timeline-scale", () => {
  describe("px ↔ seconds round-trip", () => {
    it("pxToSeconds inverts secondsToPx across scales", () => {
      for (const pps of [4, 12.5, 48, 200, 479.9]) {
        for (const s of [0, 0.1, 1, 3.75, 27, 59.9]) {
          expect(pxToSeconds(secondsToPx(s, pps), pps)).toBeCloseTo(s, 6);
        }
      }
    });

    it("secondsToPx inverts pxToSeconds across scales", () => {
      for (const pps of [4, 30, 480]) {
        for (const px of [0, 15, 120, 999]) {
          expect(secondsToPx(pxToSeconds(px, pps), pps)).toBeCloseTo(px, 6);
        }
      }
    });

    it("pxToSeconds is 0 at a zero scale (no divide-by-zero)", () => {
      expect(pxToSeconds(100, 0)).toBe(0);
    });
  });

  describe("fit", () => {
    it("fitPxPerSecond * duration exactly fills the viewport width", () => {
      for (const [w, d] of [
        [800, 8],
        [1024, 27],
        [640, 59.5],
      ]) {
        expect(scaledTrackWidth(d, fitPxPerSecond(w, d))).toBeCloseTo(w, 6);
      }
    });

    it("degenerate inputs fall back to the floor scale", () => {
      expect(fitPxPerSecond(0, 10)).toBe(MIN_PX_PER_SECOND);
      expect(fitPxPerSecond(800, 0)).toBe(MIN_PX_PER_SECOND);
    });
  });

  describe("clamp", () => {
    it("keeps the scale inside the zoom envelope", () => {
      expect(clampPxPerSecond(1)).toBe(MIN_PX_PER_SECOND);
      expect(clampPxPerSecond(9999)).toBe(MAX_PX_PER_SECOND);
      expect(clampPxPerSecond(60)).toBe(60);
    });
  });

  describe("adaptive tick density", () => {
    it("uses 1s (or finer) labels for a tightly-zoomed sub-10s clip", () => {
      // 8s across ~800px → 100px/s → 1s labels sit 100px apart.
      const interval = tickIntervalForScale(100);
      expect(interval).toBeLessThanOrEqual(1);
    });

    it("coarsens as the scale shrinks", () => {
      const dense = tickIntervalForScale(100);
      const sparse = tickIntervalForScale(6);
      expect(sparse).toBeGreaterThan(dense);
    });

    it("never places labels closer than the minimum pitch", () => {
      const minPx = 52;
      const interval = tickIntervalForScale(20, minPx);
      // Either it clears the pitch, or it's already the coarsest candidate.
      if (interval < 300) {
        expect(secondsToPx(interval, 20)).toBeGreaterThanOrEqual(minPx);
      }
    });

    it("rulerTicks spans [0, duration] and starts at 0", () => {
      const ticks = rulerTicks(8, 100);
      expect(ticks[0]).toBe(0);
      expect(ticks[ticks.length - 1]).toBeLessThanOrEqual(8);
      expect(ticks.length).toBeGreaterThan(1);
    });

    it("rulerTicks returns [0] for a zero-duration clip", () => {
      expect(rulerTicks(0, 100)).toEqual([0]);
    });
  });

  it("exposes a sane zoom envelope", () => {
    expect(MIN_PX_PER_SECOND).toBeLessThan(MAX_PX_PER_SECOND);
  });
});
