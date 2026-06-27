import { computeBarPosition } from "../bar-position";

describe("computeBarPosition", () => {
  it("returns 0 left and 100 width for full span", () => {
    expect(computeBarPosition(0, 30, 30)).toEqual({ leftPct: 0, widthPct: 100 });
  });

  it("computes correct percentages for mid-span bar", () => {
    const { leftPct, widthPct } = computeBarPosition(6, 24, 30);
    expect(leftPct).toBeCloseTo(20);
    expect(widthPct).toBeCloseTo(60);
  });

  it("clamps start below 0 to 0", () => {
    const { leftPct } = computeBarPosition(-5, 10, 30);
    expect(leftPct).toBe(0);
  });

  it("clamps end beyond totalS to totalS", () => {
    const { widthPct } = computeBarPosition(0, 40, 30);
    expect(widthPct).toBe(100);
  });

  it("applies MIN_WIDTH_PCT floor for zero-duration bar", () => {
    const { widthPct } = computeBarPosition(5, 5, 30);
    expect(widthPct).toBeGreaterThanOrEqual(1);
  });

  it("returns safe fallback when totalS <= 0", () => {
    expect(computeBarPosition(0, 10, 0)).toEqual({ leftPct: 0, widthPct: 100 });
    expect(computeBarPosition(0, 10, -1)).toEqual({ leftPct: 0, widthPct: 100 });
  });

  it("returns correct position at the end of the timeline", () => {
    const { leftPct, widthPct } = computeBarPosition(27, 30, 30);
    expect(leftPct).toBeCloseTo(90);
    expect(widthPct).toBeCloseTo(10);
  });
});
