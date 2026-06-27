import { clientXToFrac, classifyZone, clampSeconds } from "../drag-zone";

function makeRect(left: number, width: number): DOMRect {
  return {
    left,
    right: left + width,
    top: 0,
    bottom: 40,
    width,
    height: 40,
    x: left,
    y: 0,
    toJSON() { return this; },
  };
}

describe("clientXToFrac", () => {
  it("returns 0 at left edge", () => {
    expect(clientXToFrac(100, makeRect(100, 200))).toBe(0);
  });

  it("returns 1 at right edge", () => {
    expect(clientXToFrac(300, makeRect(100, 200))).toBe(1);
  });

  it("returns 0.5 at mid", () => {
    expect(clientXToFrac(200, makeRect(100, 200))).toBe(0.5);
  });

  it("clamps below 0 to 0", () => {
    expect(clientXToFrac(50, makeRect(100, 200))).toBe(0);
  });

  it("clamps above 1 to 1", () => {
    expect(clientXToFrac(400, makeRect(100, 200))).toBe(1);
  });

  it("returns 0 for zero-width rect", () => {
    expect(clientXToFrac(100, makeRect(100, 0))).toBe(0);
  });
});

describe("classifyZone", () => {
  const rect = makeRect(0, 100);

  it("returns 'left' within the left handlePx zone", () => {
    expect(classifyZone(6, rect, 12)).toBe("left");
  });

  it("returns 'right' within the right handlePx zone", () => {
    expect(classifyZone(94, rect, 12)).toBe("right");
  });

  it("returns 'body' in the middle", () => {
    expect(classifyZone(50, rect, 12)).toBe("body");
  });

  it("returns 'body' for a degenerate narrow bar (width <= 2 * handlePx)", () => {
    const narrow = makeRect(0, 20);
    expect(classifyZone(5, narrow, 12)).toBe("body");
  });

  it("uses default handlePx of 12", () => {
    expect(classifyZone(6, rect)).toBe("left");
    expect(classifyZone(50, rect)).toBe("body");
    expect(classifyZone(94, rect)).toBe("right");
  });
});

describe("clampSeconds", () => {
  it("clamps below 0 to 0", () => {
    expect(clampSeconds(-1, 30)).toBe(0);
  });

  it("clamps above maxS to maxS", () => {
    const result = clampSeconds(35, 30);
    expect(result).toBeCloseTo(30);
  });

  it("rounds to step precision", () => {
    expect(clampSeconds(5.123, 30, 0.05)).toBeCloseTo(5.1);
  });

  it("passes a clean value through unchanged", () => {
    expect(clampSeconds(10, 30)).toBe(10);
  });
});
